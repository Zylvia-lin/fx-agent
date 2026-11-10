# FX Agent 独立数据服务

本目录是独立服务端：自己的 Python 包、依赖、Docker 镜像与管理网页。本地插件只包含单机逻辑和 HTTP 客户端；两者复用明确的数据领域模块，服务端镜像不包含本地 MCP 入口、DCC 桥接和客户端安装器。

管理页中的项目代表完整数据空间。现有全部数据属于 `default`，内部工程/镜头的 `project_id` 不参与 Key 授权。一个 Key 可关联多个空间；多空间 Key 需通过 `X-FX-Data-Project` 或客户端 `FX_AGENT_DATA_PROJECT` 选择当前空间。单空间 Key 自动选择；删除 Key 不删除项目及数据。默认空间原目录及 MySQL 表保持原样，新空间使用独立目录和同库表前缀，自动备份覆盖所有已登记空间。详见[共享模式说明](../../public-docs/多设备共享数据服务.md)。

## 已有 MySQL + Docker

要求 Linux Docker/Compose、Python 3.12 镜像、已有的 MySQL 5.7+ 数据库和专用账号。Compose 只有数据服务一个容器，直接通过 host 网络访问本机 MySQL，不增加数据库容器。

1. 将已有 `data.zip` 安全解压到 `/www/fx-agent/data`，保留原 ZIP。
2. 创建仅管理员可读的 API Key、管理员密码和 MySQL 密码文件；API Key 可用 `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` 生成。
3. 参照 `.env.example` 配置绝对数据路径、MySQL 用户/数据库及私密文件路径。不要将真实凭据提交到 Git。
4. 在仓库根执行：

```sh
docker compose --env-file /www/fx-agent/deployment.env \
  -f services/data-service/compose.yaml up -d --build
```

如果本机使用独立 `docker-compose` 命令，可等价替换。构建默认使用 PyPI，可用 `PIP_INDEX_URL` 指定部署网络可访问的 HTTPS 包镜像。

当前服务器的部署配置保存在 `/www/fx-agent/deployment.env`。在发布源码目录运行 Compose 时显式传入 `--env-file /www/fx-agent/deployment.env`；该文件为 Compose 提供 MySQL 用户和各 secret 文件路径。`required variable ... is missing a value` 表示 Compose 尚未载入这些变量，容器还未启动，不代表 MySQL 连接失败。

容器首次启动会把现有 `data/memory.sqlite3` 逐表迁移到专用 MySQL 数据库，保留记录内容、ID、修订及分页行序，并校验各表行数。迁移使用一个数据事务，拒绝覆盖非空的目标知识表。迁移标记确保重启不会重复导入。原 SQLite 和 ZIP 保留，案例与模板归档继续从挂载的数据目录读取。

服务内部只监听 `127.0.0.1:18790`。生产环境用 Nginx/Caddy 提供 HTTPS；当前部署外部端口为 `18443`，`/admin` 为管理员网页，`/v1/*` 为认证数据接口。不要代理 `/internal/*`，它仅用于容器自身健康检查。

## 环境变量

| 变量 | 用途 |
|---|---|
| `FX_AGENT_HOST` / `FX_AGENT_PORT` | 服务监听地址/端口 |
| `FX_AGENT_DATA_ROOT` | 案例、模板、知识文档目录，容器默认 `/data` |
| `FX_AGENT_DATABASE_ROOT` | 本机服务锁与兼容 SQLite 工作目录，默认 `/database` |
| `FX_AGENT_DB_ENGINE` | 默认 `mysql`；本地插件的单机模式不受此项影响 |
| `FX_AGENT_MYSQL_HOST` / `FX_AGENT_MYSQL_PORT` | 已有 MySQL 地址与端口 |
| `FX_AGENT_MYSQL_USER` / `FX_AGENT_MYSQL_DATABASE` | 专用账号与数据库 |
| `FX_AGENT_MYSQL_PASSWORD` 或 `_FILE` | 密码或密码文件路径 |
| `FX_AGENT_API_KEY` 或 `_FILE` | 环境变量主 Key 或其私密文件 |
| `FX_AGENT_ADMIN_PASSWORD` 或 `_FILE` | 独立网页管理员密码；不设置则关闭管理网页 |
| `FX_AGENT_BACKUP_ROOT` | 可选备份目录，默认数据目录下 `backups/` |
| `PIP_INDEX_URL` | 镜像构建使用的 Python 包源 |

同一敏感项同时设置时，`_FILE` 优先。Docker Compose 将宿主机的私密文件作为 secrets 挂载，不把内容写进镜像。

## 管理网页和 API Key

访问 `https://服务端IP:18443/admin`，用管理员密码登录，可以创建完整数据空间、创建命名 Key 时多选关联空间，并通过“保存关联”调整范围。同一数据空间可以关联多个 Key。管理页中的项目 ID 与工具参数中的制作工程 `project_id` 属于不同层级；现有全部工程、经验和归档只对应一个 `default` 项目。

项目独立于访问凭据保存。“删除 Key”会永久禁用该凭据，保留项目、进度和所有数据；新 Key 可以重新关联原项目。完整 Key 只在创建时返回一次；数据库只存哈希。主环境 Key 也可以被禁用，重启不会自动启用它；修改环境 Key 的值表示主动轮换，保留已配置的项目范围。

Key 只能访问关联的数据空间；移除关联后，历史请求缓存也不能继续访问该空间。未关联空间的 Key 无法访问数据。旧凭据一次性归入默认空间，重启保留之后的关联调整。单空间 Key 自动选择，多空间 Key 必须提供 `X-FX-Data-Project` 请求头或客户端 `FX_AGENT_DATA_PROJECT`。

管理接口：`GET/POST /admin/api/projects` 查询或创建完整数据空间（创建传 `id`）；`POST /admin/api/keys` 接收 `name` 和 `project_ids` 数组；`PATCH /admin/api/keys/{id}` 替换 `project_ids`；`DELETE /admin/api/keys/{id}` 撤销凭据。`project_ids` 返回授权空间列表，`/v1/status` 的 `data_project` 返回当前空间。

同一数据空间内经验、模板、归档和全局规范共用；不同空间的数据、文件及请求缓存独立。设备 ID 用于区分同一空间内的日常任务与进度，轮换 Key 时应保留设备 ID。

登录有速率限制；管理员 Cookie 使用 Secure、HttpOnly 和 SameSite=Strict，管理写接口检查请求来源。应通过 HTTPS 访问；自签名证书需将专用 CA/证书交给客户端信任。

登录后有效期固定为 8 小时，访问不会续期；浏览器 Max-Age 与服务端签名时间均参与校验。“退出登录”调用 `POST /admin/api/logout` 清除本机 Cookie、页面 Key 和敏感输入，并忽略退出前未完成请求的响应。

## 备份和验证

启动时及每 24 小时在同一操作边界内导出 MySQL 知识与服务状态的压缩 JSON 快照。状态接口显示最近备份或错误；历史备份不自动删除。归档文件、原 ZIP、配置和私密文件应单独保管。恢复数据库应先停服务并由管理员核对备份及对应文件，不能把数据库回退当作文件也已回退。

上线验收包括：原数据库各表行数、经验与规范查询、模块标签过滤、案例/模板引用校验、版本冲突拒绝、缺少/错误 API Key 返回 401、管理员登录与 Key 创建/禁用、两设备共用 Key 的逻辑任务区分。
