# FX Agent 独立数据服务

本目录是独立服务端：自己的 Python 包、依赖、Docker 镜像与管理网页。本地插件只包含单机逻辑和 HTTP 客户端；两者复用明确的数据领域模块，服务端镜像不包含本地 MCP 入口、DCC 桥接和客户端安装器。

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

访问 `https://服务端IP:18443/admin`，用管理员密码登录，可以创建项目、创建命名 Key 时多选关联项目，并通过“保存关联”调整已有 Key 的项目范围。同一项目可以关联多个 Key。项目 ID 应与插件请求中的 `project_id` 一致；已有进度、任务、经验、项目规范和案例中的项目会自动出现在目录中。

项目独立于访问凭据保存。“删除 Key”会永久禁用该凭据，保留项目、进度和所有数据；新 Key 可以重新关联原项目。完整 Key 只在创建时返回一次；数据库只存哈希。主环境 Key 也可以被禁用，重启不会自动启用它；修改环境 Key 的值表示主动轮换，保留已配置的项目范围。

新 Key 只能操作关联项目，包括嵌套参数中的项目和仅凭任务 ID 发起的访问；移除关联后，历史请求缓存也不能继续访问该项目。未选择项目的新 Key 仅可使用公共知识。升级前已有 Key 保留“全部项目”的兼容访问，保存关联后改为仅允许所选项目；配置文件中的旧设备凭据仍保留原有访问范围。

管理接口：`GET/POST /admin/api/projects` 查询或创建项目（创建传 `id`）；`POST /admin/api/keys` 接收 `name` 和 `project_ids` 数组；`PATCH /admin/api/keys/{id}` 替换 `project_ids`；`DELETE /admin/api/keys/{id}` 撤销凭据。Key 列表与 `/v1/status` 返回的 `project_ids` 为 `null` 表示旧版兼容访问，为 `[]` 表示未关联任何项目。

公共经验、模板、归档素材和全局规范仍作为共享知识复用，项目关联不提供独立租户级隔离。设备 ID 只防止日常任务误冲突，不能阻止持 Key 的团队成员主动冒充另一设备；项目进度和任务仍按设备区分，轮换 Key 时应保留原设备 ID。

登录有速率限制；管理员 Cookie 使用 Secure、HttpOnly 和 SameSite=Strict，管理写接口检查请求来源。应通过 HTTPS 访问；自签名证书需将专用 CA/证书交给客户端信任。

## 备份和验证

启动时及每 24 小时在同一操作边界内导出 MySQL 知识与服务状态的压缩 JSON 快照。状态接口显示最近备份或错误；历史备份不自动删除。归档文件、原 ZIP、配置和私密文件应单独保管。恢复数据库应先停服务并由管理员核对备份及对应文件，不能把数据库回退当作文件也已回退。

上线验收包括：原数据库各表行数、经验与规范查询、模块标签过滤、案例/模板引用校验、版本冲突拒绝、缺少/错误 API Key 返回 401、管理员登录与 Key 创建/禁用、两设备共用 Key 的逻辑任务区分。
