# Open-AutoGLM（手机 AI 助手/Phone Agent）

本仓库已引入 `Open-AutoGLM/` 作为二次开发基线，用于实现“像豆包手机助手一样”的手机 AI 自动化助手：

- **输入**：自然语言指令（后续可扩展语音）
- **输出**：通过 ADB 自动操作 Android 手机（点按/滑动/输入/返回/启动 App 等）

## 快速开始

进入目录按上游文档安装与运行：

```bash
cd Open-AutoGLM
pip install -r requirements.txt
pip install -e .
python main.py --base-url <你的模型服务URL> --model <模型名> "打开微信发消息给文件传输助手：你好"
```

## 二次开发调试：Trace 导出（已加）

为了方便复盘与排障，支持按步骤导出 trace：

```bash
cd Open-AutoGLM
python main.py --trace ./runs/trace.jsonl "打开微信发消息给文件传输助手：你好"
```

- `--trace`：写入 JSONL（每行一个 step 事件）
- `--trace-no-screenshots`：不保存截图
- `--trace-screenshot-dir`：指定截图保存目录