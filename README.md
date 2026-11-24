# Shared Helm Values Cleanup API

生产仍在用旧版 Helm Chart（v1），预发已切到新版（v2），而两边共用同一份 values
覆盖文件。本服务用 **FastAPI** 找出覆盖文件里那些只是重复 Chart 默认值、删掉后
**每个版本** 渲染结果都保持不变的映射键，并返回清理后的 YAML 和删键路径；其余覆盖
原样保留。

## 目录结构

```
mounted-charts/                 # 模拟“挂载目录”：两个渲染结果稳定的 Chart
├── orders-api/
│   ├── v1/                     # 旧版（生产，chart 1.4.0 / app 1.4.2）
│   └── v2/                     # 新版（预发，chart 2.1.0 / app 2.1.0，含 HPA 等）
└── payment-web/
    ├── v1/                     # 旧版（生产，chart 1.8.0）
    └── v2/                     # 新版（预发，chart 2.0.0，含 nodeSelector/ConfigMap/ServiceMonitor）
overrides/                      # 两版共用的 values 覆盖（待清理对象）
├── orders-api.values.yaml
└── payment-web.values.yaml
app/                            # FastAPI 服务
├── main.py                     # 路由、请求模型、Swagger 双样例
├── solver.py                   # 基线渲染 → 单键筛选 → 最多 8 键组合穷举 → 复核
├── helm.py                     # helm template 封装：stdout 固定块流式落盘
├── baseline.py                 # 每版一份的磁盘 SQLite 基线，逐文档流式比对
├── staging.py                  # 请求级暂存目录，所有读写失败统一抛 StagingError
├── normalize.py                # 文档流式解析、资源标识与规范 YAML 序列化
└── values_tools.py             # 叶键枚举、路径表示、删键
tests/                          # pytest 自测（48 个用例）
Dockerfile
docker-compose.yml
requirements.txt
examples/                       # Swagger 两个样例对应的原始请求体
```

## 清理规则与算法

1. **基线渲染（每版仅一次）**：用固定发布名 `platform-rel` 和命名空间 `platform`
   对每个版本执行 `helm template -f <覆盖文件>`，helm 的 stdout 以固定大小块
   直接写入请求级暂存文件，再逐文档写入该版本专属的 SQLite 基线库。任一版本
   基线渲染失败（含 helm 非零退出、重复资源标识）→ 直接报错（HTTP 422）。
   后续单键试删、组合穷举和最终复核全部复用同一份基线，绝不重新生成。
2. **叶键枚举**：只枚举映射键的叶路径（标量/列表终止），天然互不包含；
   形如 `image.pullPolicy`，带点的键用引号段表示（如
   `podAnnotations."checksum/legacy"`）。
3. **单键筛选**：逐个删键试渲染，只有单独删除后所有版本仍渲染成功且清单与基线
   语义一致的键才进入候选——避免把 8 个名额浪费在不可能删除的键上。
4. **候选上限 8 个**：可删键超过 8 个时按覆盖文件中的顺序取前 8（响应中
   `candidate_cap_applied=true`）。
5. **组合穷举**：从最大子集开始尝试，找到能同时删除的最多候选键即返回。
   某组合渲染失败不会中断流程，自动跳过并尝试其他组合（`render_failures` 计数）。
6. **最终复核**：用清理后的 YAML 对每个版本重新渲染并与**同一份基线**比对，
   全部一致才置 `verified=true`。

### 暂存与失败约定

- 每个请求拥有独立临时目录（`Staging`），请求结束即整体删除；试渲染产生的
  values 与 manifest 文件在**下一轮开始前**就被删除，不会跨轮累积。
- 暂存目录的任何读写/删除失败都抛 `StagingError` → HTTP 500，且**不返回清理结果**。
- helm 非零退出一律按其所在阶段的渲染失败处理：即使它同时输出了与成功渲染
  逐字节相同的清单，退出码仍优先（基线阶段 → 422；试删阶段 → 跳过该组合）。

### 内存模型（不随文档总数增长）

在版本数固定、最大单文档大小固定的前提下，从接收 helm 输出到完成比对，
服务进程内存与渲染出的文档总数无关：

| 环节 | 旧实现 | 现实现 |
| --- | --- | --- |
| helm 输出 | `capture_output` 整份 stdout 字符串驻留内存 | 子进程 stdout 重定向到文件，固定块流式写盘 |
| 基线留存 | 每版完整渲染字符串 + 解析后的 Python 对象列表 | 每版一个 SQLite 文件，页缓存有上限，其余落盘 |
| 文档比对 | 两份完整清单同时载入、排序后比较 | `safe_load_all` 逐文档解析（PyYAML 4KB 分块读取并释放已消费缓冲），逐文档查库 |
| 试渲染暂存 | 同名文件反复覆盖，旧内容滞留 | 每轮唯一命名并在轮末删除，磁盘与内存均不累积 |

实测（单文档约 4KB，文档数 100→8000，旧/新两条管线各跑独立进程）见
下文“自测”一节：旧管线 Python 堆峰值随文档数线性增长，新管线保持恒定
（约 0.1MB 量级），RSS 也基本持平。

### 比对语义

- 忽略 YAML 注释（解析为对象后注释不存在）
- 忽略文档先后：以稳定资源标识 `kind/namespace/name` 为主键逐文档匹配，
  清单内标识必须唯一；出现重复标识时基线阶段直接报错（无法建立一一对应）
- 忽略映射键顺序：每文档以排序键的规范 YAML 落库并按内容比较
- **列表顺序必须相同**：列表按解析顺序序列化、逐字节比较，顺序不同即不一致

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# Swagger UI（可直接试两个样例）： http://localhost:8000/docs
# OpenAPI JSON：                    http://localhost:8000/openapi.json
# 健康检查：                        http://localhost:8000/health
```

镜像内安装了 Helm 3，并把 `mounted-charts/`、`overrides/` 以只读卷挂到
`/workspace/...`，与示例请求里的绝对路径一致。

### 本地直跑（无 Docker 时）

```bash
# 依赖 Helm 3 在 PATH 上，Python 依赖见 requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### `POST /api/v1/solve`

请求体：

| 字段 | 说明 |
| --- | --- |
| `values_override_path` | 共用覆盖 YAML 的绝对路径 |
| `versions[]` | 每个版本的 `label`（唯一）与 `chart_dir`（绝对路径） |
| `release_name` | 固定发布名，默认 `platform-rel` |
| `namespace` | 固定命名空间，默认 `platform` |

响应关键字段：`cleaned_yaml`（删键后的覆盖）、`removed_paths`（可删路径）、
`candidate_paths`（最多 8 个候选）、`kept_paths`（保留的其余叶键）、
`versions[].manifests_match_baseline`、`verified`。

两个可在 Swagger 直接试用的样例（请求体同时保存在 `examples/`）：

- `orders-api`：21 个叶键，**8 个可全部删除**；跨版默认值翻转的
  `podSecurityContext.*` 被正确保留。
- `payment-web`：18 个叶键，其中 10 个单独可删，受 8 键上限约束取前 8；
  v2 独有的 `nodeSelector.disktype`（v1 模板忽略它，看似可删）被正确保留。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/solve \
  -H 'Content-Type: application/json' \
  --data @examples/orders-api.request.json | jq .
# 想要 YAML 响应：加 -H 'Accept: application/yaml'
```

错误约定：覆盖文件不存在/无法解析 → 400；基线 `helm template` 失败（含非零退出、
重复资源标识）→ 422（附 helm stderr）；暂存目录读写失败 → 500，不返回清理结果。

## 自测

```bash
PATH=/path/to/helm:$PATH python3 -m pytest tests/ -q
# 当前实测：48 passed
```

覆盖：归一化/流式比对规则（乱序等价、列表有序、标量差异、缺失/多余文档、
基线与试渲染两侧的重复标识拒绝、500 文档流式往返）、路径工具、暂存目录
生命周期与故障、helm 流式封装（非零退出优先于相同输出、超时、可执行文件
缺失、试渲染期 helm 不可用按失败计数而非中断请求）、两个 Chart 的端到端
清理、8 键上限、跨版陷阱键保留、基线失败报错、删键组合渲染失败自动跳过、
乱序文档/键仍判定一致、列表乱序判定不一致、API 200/400/422/500 与
Swagger 双样例。

### 内存对比

仓库外的基准脚本（不随镜像交付）以固定单文档大小（约 4KB/份）、递增文档
总数生成两份仅文档顺序不同的清单，分别走旧的“全量驻留”管线和新的
“流式 + SQLite”管线，每个数据点独立进程，取 `getrusage` 峰值 RSS：

```text
docs   mode   file_mb   peak_rss_mb   equal
100    old       0.4          14.8    True
100    new       0.4          13.6    True
500    old       2.0          24.6    True
500    new       2.0          15.2    True
1000   old       4.1          37.1    True
1000   new       4.1          15.2    True
2000   old       8.2          61.0    True
2000   new       8.2          15.2    True
4000   old      16.4         109.2    True
4000   new      16.4          15.4    True
8000   old      32.8         172.0    True
8000   new      32.8          15.3    True
```

旧管线峰值 RSS 随文档数线性上升（8000 份时 172MB），新管线恒定在约 15MB
（解释器 + 固定 SQLite 页缓存 + 单文档工作集），与文档总数无关；
tracemalloc 口径下新管线 Python 堆峰值同样恒定（约 0.1MB/次比对），
旧管线则线性增长（1000 份时 19.5MB）。所有数据点的乱序清单比对结果均为
`True`，语义正确性未因流式化改变。
