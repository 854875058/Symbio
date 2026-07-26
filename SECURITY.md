# 安全策略

## 报告漏洞

如果你发现 Symbio 中的安全漏洞，请通过以下方式报告：

**请不要在公开 Issue 中报告安全漏洞。**

### 报告方式

1. **GitHub Security Advisory**: 使用 GitHub 的私有漏洞报告功能
2. **邮件**: 发送至 [security@symbio.dev]（示例邮箱）

### 报告内容

请包含以下信息：

- 漏洞描述
- 复现步骤
- 影响范围
- 潜在风险等级
- 建议修复方案（如有）

### 响应时间

- **确认收到**: 24 小时内
- **初步评估**: 72 小时内
- **修复计划**: 1 周内
- **修复发布**: 根据严重程度，1-4 周内

## 支持的版本

| 版本 | 支持状态 |
|------|----------|
| 最新版本 | ✅ 支持 |
| 前一个主版本 | ⚠️ 安全修复 |
| 更早版本 | ❌ 不支持 |

## 安全最佳实践

### 部署安全

1. **API Key 管理**
   - 不要将 API Key 硬编码在代码中
   - 使用环境变量或密钥管理服务
   - 定期轮换 API Key

2. **API 鉴权（对外暴露前必做）**
   - Symbio 的 API 默认**不启用**鉴权，因为默认只监听 `127.0.0.1`（单机本地使用）。
   - 一旦绑定到 `0.0.0.0` 或放到反向代理后面，**必须**先设置 token：

     ```bash
     export SYMBIO_API_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
     ```

     也可在 `symbio.yaml` 里配 `server.api_token`。设置后所有 `/api/*` 与
     WebSocket 都需要 `Authorization: Bearer <token>`（浏览器 WebSocket 用
     `?token=`）；仅健康检查、`/.well-known/agent.json` 和 UI 静态资源公开。
   - 未配 token 就绑定所有网卡时，启动会打印红色告警 —— 不要忽略它。
     此时 `POST /api/sandbox/execute` 和 `WS /ws/terminal` 等于把命令执行权
     开放给同网段的任何人。

3. **网络安全**
   - 使用 HTTPS 进行所有通信
   - 配置适当的 CORS 策略（默认仅 localhost；`cors_origins` 设为 `*` 时会自动关闭 credentials）
   - 使用防火墙限制访问

4. **数据安全**
   - 敏感数据加密存储
   - 定期备份数据
   - 遵循数据最小化原则

### 运行时安全

1. **沙箱隔离**
   - 工具执行在沙箱中运行
   - 限制文件系统访问
   - 限制网络访问

2. **权限控制**
   - 遵循最小权限原则
   - 高危操作需要人工审批
   - 记录所有操作日志

3. **Prompt Injection 防护**
   - 输入净化和验证
   - 输出审计和过滤
   - 行为偏离检测

## 安全审计

### 自动化安全检查

- 依赖漏洞扫描 (Dependabot)
- 代码安全分析 (CodeQL)
- 容器镜像扫描 (Trivy)

### 定期安全审计

- 每季度进行一次安全审计
- 年度第三方安全评估
- 渗透测试（按需）

## 安全更新

安全更新将通过以下方式发布：

1. **GitHub Security Advisory**: 发布安全公告
2. **GitHub Releases**: 发布修复版本
3. **邮件通知**: 通知注册用户
4. **社交媒体**: 发布安全公告

## 致谢

我们感谢以下安全研究人员的贡献：

<!-- 在此添加安全研究人员名单 -->

## 联系方式

- 安全问题: [security@symbio.dev]
- 一般问题: GitHub Issues
- 实时交流: Discord
