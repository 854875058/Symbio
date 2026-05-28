# 贡献指南

感谢你对 Symbio 项目的关注！我们欢迎任何形式的贡献。

## 如何贡献

### 报告 Bug

1. 在 [Issues](https://github.com/854875058/Symbio/issues) 页面创建新 Issue
2. 使用 Bug Report 模板
3. 提供详细的复现步骤、期望行为、实际行为
4. 附上相关日志和截图

### 提出新功能

1. 在 [Issues](https://github.com/854875058/Symbio/issues) 页面创建新 Issue
2. 使用 Feature Request 模板
3. 描述功能的使用场景和预期效果

### 提交代码

1. Fork 项目
2. 创建功能分支：`git checkout -b feature/your-feature`
3. 提交代码：`git commit -m 'feat: add your feature'`
4. 推送分支：`git push origin feature/your-feature`
5. 创建 Pull Request

## 开发环境

```bash
# 克隆项目
git clone https://github.com/854875058/Symbio.git
cd Symbio

# 安装依赖
pip install -e ".[dev]"

# 运行测试
pytest

# 代码检查
ruff check .
ruff format .
```

## 代码规范

### 提交信息格式

遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <subject>

<body>

<footer>
```

类型：
- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `style`: 代码格式调整
- `refactor`: 代码重构
- `test`: 测试相关
- `chore`: 构建/工具相关

### 代码风格

- 使用 Ruff 进行代码格式化
- 类型注解：所有公共函数必须有类型注解
- 文档字符串：所有公共类和函数必须有文档字符串
- 测试覆盖：新功能必须包含测试

### 分支策略

- `main`: 稳定版本
- `develop`: 开发版本
- `feature/*`: 功能分支
- `fix/*`: 修复分支
- `release/*`: 发布分支

## Pull Request 规范

### PR 标题

遵循提交信息格式：`<type>(<scope>): <subject>`

### PR 内容

使用 PR 模板，包含：
- 变更描述
- 关联 Issue
- 测试说明
- 截图（如有 UI 变更）

### Review 流程

1. 至少需要 1 位维护者 Review
2. 所有 CI 检查必须通过
3. 没有合并冲突

## 行为准则

请遵守我们的 [行为准则](CODE_OF_CONDUCT.md)。

## 联系方式

- GitHub Issues: 项目相关问题
- Discord: 实时交流
- 微信群: 中文社区

## 许可证

贡献的代码将在 [MIT License](LICENSE) 下发布。
