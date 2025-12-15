"""Main PhoneAgent class for orchestrating phone automation."""

import base64
import json
import os
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from phone_agent.actions import ActionHandler
from phone_agent.actions.handler import do, finish, parse_action
from phone_agent.adb import get_current_app, get_screenshot, get_ui_hierarchy_compact
from phone_agent.config import get_messages, get_system_prompt
from phone_agent.model import ModelClient, ModelConfig
from phone_agent.model.client import MessageBuilder


@dataclass
class AgentConfig:
    """Configuration for the PhoneAgent."""

    max_steps: int = 100
    device_id: str | None = None
    lang: str = "cn"
    system_prompt: str | None = None
    verbose: bool = True
    # Optional trace export for secondary development & debugging
    trace_path: str | None = None  # JSONL file path
    trace_save_screenshots: bool = True
    trace_screenshot_dir: str | None = None  # If None, derive from trace_path
    # Robustness knobs
    auto_takeover_on_sensitive_screen: bool = True
    auto_retry_on_no_change: bool = True
    auto_retry_max: int = 1
    auto_retry_wait_s: float = 1.0
    include_ui_hierarchy: bool = True
    ui_hierarchy_max_nodes: int = 180
    # If the agent sees "no change" repeatedly, do a generic recovery
    stuck_no_change_threshold: int = 3

    def __post_init__(self):
        if self.system_prompt is None:
            self.system_prompt = get_system_prompt(self.lang)


@dataclass
class StepResult:
    """Result of a single agent step."""

    success: bool
    finished: bool
    action: dict[str, Any] | None
    thinking: str
    message: str | None = None


class PhoneAgent:
    """
    AI-powered agent for automating Android phone interactions.

    The agent uses a vision-language model to understand screen content
    and decide on actions to complete user tasks.

    Args:
        model_config: Configuration for the AI model.
        agent_config: Configuration for the agent behavior.
        confirmation_callback: Optional callback for sensitive action confirmation.
        takeover_callback: Optional callback for takeover requests.

    Example:
        >>> from phone_agent import PhoneAgent
        >>> from phone_agent.model import ModelConfig
        >>>
        >>> model_config = ModelConfig(base_url="http://localhost:8000/v1")
        >>> agent = PhoneAgent(model_config)
        >>> agent.run("Open WeChat and send a message to John")
    """

    def __init__(
        self,
        model_config: ModelConfig | None = None,
        agent_config: AgentConfig | None = None,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.model_config = model_config or ModelConfig()
        self.agent_config = agent_config or AgentConfig()

        self.model_client = ModelClient(self.model_config)
        self.action_handler = ActionHandler(
            device_id=self.agent_config.device_id,
            confirmation_callback=confirmation_callback,
            takeover_callback=takeover_callback,
        )

        self._context: list[dict[str, Any]] = []
        self._step_count = 0
        self._no_change_streak = 0

    def _append_trace_event(self, event: dict[str, Any]) -> None:
        """Append a single trace event as JSONL (best-effort)."""
        if not self.agent_config.trace_path:
            return
        try:
            trace_path = self.agent_config.trace_path
            os.makedirs(os.path.dirname(os.path.abspath(trace_path)) or ".", exist_ok=True)
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            # Tracing must never break the agent loop
            if self.agent_config.verbose:
                traceback.print_exc()

    def _maybe_save_screenshot_for_trace(self, screenshot_b64: str, step: int) -> str | None:
        """Save current screenshot to disk for trace, return file path if saved."""
        if not self.agent_config.trace_path:
            return None
        if not self.agent_config.trace_save_screenshots:
            return None

        try:
            trace_path = os.path.abspath(self.agent_config.trace_path)
            base_dir = os.path.dirname(trace_path) or "."
            screenshot_dir = self.agent_config.trace_screenshot_dir
            if not screenshot_dir:
                stem = os.path.splitext(os.path.basename(trace_path))[0] or "trace"
                screenshot_dir = os.path.join(base_dir, f"{stem}_screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)

            filename = f"step_{step:04d}.png"
            out_path = os.path.join(screenshot_dir, filename)
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(screenshot_b64))
            return out_path
        except Exception:
            if self.agent_config.verbose:
                traceback.print_exc()
            return None

    @staticmethod
    def _screen_fingerprint(screenshot_b64: str, current_app: str) -> str:
        """
        Cheap fingerprint to detect if screen likely changed.

        We avoid heavy image diff; base64 prefix is usually enough to catch changes.
        """
        prefix = screenshot_b64[:512] if screenshot_b64 else ""
        return f"{current_app}|{prefix}"

    def run(self, task: str) -> str:
        """
        Run the agent to complete a task.

        Args:
            task: Natural language description of the task.

        Returns:
            Final message from the agent.
        """
        self._context = []
        self._step_count = 0

        # First step with user prompt
        result = self._execute_step(task, is_first=True)

        if result.finished:
            return result.message or "Task completed"

        # Continue until finished or max steps reached
        while self._step_count < self.agent_config.max_steps:
            result = self._execute_step(is_first=False)

            if result.finished:
                return result.message or "Task completed"

        return "Max steps reached"

    def step(self, task: str | None = None) -> StepResult:
        """
        Execute a single step of the agent.

        Useful for manual control or debugging.

        Args:
            task: Task description (only needed for first step).

        Returns:
            StepResult with step details.
        """
        is_first = len(self._context) == 0

        if is_first and not task:
            raise ValueError("Task is required for the first step")

        return self._execute_step(task, is_first)

    def reset(self) -> None:
        """Reset the agent state for a new task."""
        self._context = []
        self._step_count = 0
        self._no_change_streak = 0

    def _execute_step(
        self, user_prompt: str | None = None, is_first: bool = False
    ) -> StepResult:
        """Execute a single step of the agent loop."""
        self._step_count += 1
        step_start_time = time.time()

        # Capture current screen state
        screenshot = get_screenshot(self.agent_config.device_id)
        current_app = get_current_app(self.agent_config.device_id)
        ui_nodes: list[dict[str, str]] = []
        if self.agent_config.include_ui_hierarchy and not screenshot.is_sensitive:
            ui_nodes = get_ui_hierarchy_compact(
                self.agent_config.device_id, max_nodes=self.agent_config.ui_hierarchy_max_nodes
            )
        screenshot_file = self._maybe_save_screenshot_for_trace(
            screenshot.base64_data, self._step_count
        )

        # If we're on a sensitive/black screen, ask for takeover rather than looping blindly.
        if screenshot.is_sensitive and self.agent_config.auto_takeover_on_sensitive_screen:
            takeover_action = do(
                action="Take_over",
                message="检测到敏感页面/截图黑屏（如支付/密码/银行页），请手动完成后继续。",
            )
            self._append_trace_event(
                {
                    "event": "sensitive_screen_takeover",
                    "step": self._step_count,
                    "ts": time.time(),
                    "current_app": current_app,
                    "screenshot": {
                        "path": screenshot_file,
                        "width": screenshot.width,
                        "height": screenshot.height,
                        "is_sensitive": True,
                    },
                }
            )
            result = self.action_handler.execute(
                takeover_action, screenshot.width, screenshot.height
            )
            return StepResult(
                success=result.success,
                finished=False,
                action=takeover_action,
                thinking="",
                message=result.message,
            )

        # Build messages
        if is_first:
            self._context.append(
                MessageBuilder.create_system_message(self.agent_config.system_prompt)
            )

            screen_info = MessageBuilder.build_screen_info(current_app, ui=ui_nodes)
            text_content = f"{user_prompt}\n\n{screen_info}"

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )
        else:
            screen_info = MessageBuilder.build_screen_info(current_app, ui=ui_nodes)
            text_content = f"** Screen Info **\n\n{screen_info}"

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )

        # Trace: input snapshot (no images embedded)
        self._append_trace_event(
            {
                "event": "step_start",
                "step": self._step_count,
                "ts": step_start_time,
                "is_first": is_first,
                "user_prompt": user_prompt if is_first else None,
                "current_app": current_app,
                "screen_info": screen_info,
                "screenshot": {
                    "path": screenshot_file,
                    "width": screenshot.width,
                    "height": screenshot.height,
                    "is_sensitive": screenshot.is_sensitive,
                },
            }
        )

        # Get model response
        try:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "=" * 50)
            print(f"💭 {msgs['thinking']}:")
            print("-" * 50)
            response = self.model_client.request(self._context)
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            self._append_trace_event(
                {
                    "event": "model_error",
                    "step": self._step_count,
                    "ts": time.time(),
                    "error": str(e),
                }
            )
            return StepResult(
                success=False,
                finished=True,
                action=None,
                thinking="",
                message=f"Model error: {e}",
            )

        # Parse action from response
        try:
            action = parse_action(response.action)
        except ValueError:
            if self.agent_config.verbose:
                traceback.print_exc()
            action = finish(message=response.action)

        if self.agent_config.verbose:
            # Print thinking process
            print("-" * 50)
            print(f"🎯 {msgs['action']}:")
            print(json.dumps(action, ensure_ascii=False, indent=2))
            print("=" * 50 + "\n")

        # Remove image from context to save space
        self._context[-1] = MessageBuilder.remove_images_from_message(self._context[-1])

        # Execute action
        try:
            result = self.action_handler.execute(
                action, screenshot.width, screenshot.height
            )
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            result = self.action_handler.execute(
                finish(message=str(e)), screenshot.width, screenshot.height
            )

        # Robustness: if a UI action likely didn't take effect, auto-wait and retry once.
        if (
            self.agent_config.auto_retry_on_no_change
            and action.get("_metadata") == "do"
            and self.agent_config.auto_retry_max > 0
        ):
            action_name = action.get("action")
            should_verify = action_name in {
                "Tap",
                "Swipe",
                "Launch",
                "Back",
                "Home",
                "Double Tap",
                "Long Press",
            }
            if should_verify and result.success:
                before_fp = self._screen_fingerprint(screenshot.base64_data, current_app)
                # Give the UI a short chance to update before checking
                time.sleep(0.2)
                after = get_screenshot(self.agent_config.device_id)
                after_app = get_current_app(self.agent_config.device_id)
                after_fp = self._screen_fingerprint(after.base64_data, after_app)

                if after_fp == before_fp:
                    self._no_change_streak += 1
                    self._append_trace_event(
                        {
                            "event": "no_change_detected",
                            "step": self._step_count,
                            "ts": time.time(),
                            "action_name": action_name,
                            "streak": self._no_change_streak,
                        }
                    )
                    # Wait then retry the same action once (best-effort)
                    time.sleep(self.agent_config.auto_retry_wait_s)
                    retry_action = action
                    # For Tap-like actions, slightly adjust tap point to avoid dead zones
                    if action_name in {"Tap", "Double Tap", "Long Press"} and isinstance(
                        action.get("element"), list
                    ):
                        try:
                            x, y = action["element"][:2]
                            retry_action = dict(action)
                            retry_action["element"] = [
                                max(0, min(999, int(x) + 10)),
                                max(0, min(999, int(y) + 10)),
                            ]
                        except Exception:
                            retry_action = action

                    retry_result = self.action_handler.execute(
                        retry_action, screenshot.width, screenshot.height
                    )
                    self._append_trace_event(
                        {
                            "event": "auto_retry_executed",
                            "step": self._step_count,
                            "ts": time.time(),
                            "action_name": action_name,
                            "retry_action": retry_action,
                            "retry_result": {
                                "success": retry_result.success,
                                "should_finish": retry_result.should_finish,
                                "message": retry_result.message,
                            },
                        }
                    )
                    # Prefer retry outcome if it improved, but do not force-finish here.
                    if retry_result.success:
                        result = retry_result
                else:
                    self._no_change_streak = 0

        # Generic recovery if stuck: one Back to escape overlays / dead ends
        if (
            self.agent_config.stuck_no_change_threshold > 0
            and self._no_change_streak >= self.agent_config.stuck_no_change_threshold
        ):
            self._append_trace_event(
                {
                    "event": "stuck_recovery_back",
                    "step": self._step_count,
                    "ts": time.time(),
                    "streak": self._no_change_streak,
                }
            )
            try:
                self.action_handler.execute(
                    do(action="Back"), screenshot.width, screenshot.height
                )
            except Exception:
                pass
            self._no_change_streak = 0

        # Trace: step outcome
        self._append_trace_event(
            {
                "event": "step_end",
                "step": self._step_count,
                "ts": time.time(),
                "elapsed_s": round(time.time() - step_start_time, 6),
                "model": {
                    "thinking": response.thinking,
                    "action": response.action,
                    "raw_content": response.raw_content,
                    "time_to_first_token": response.time_to_first_token,
                    "time_to_thinking_end": response.time_to_thinking_end,
                    "total_time": response.total_time,
                },
                "parsed_action": action,
                "execution": {
                    "success": result.success,
                    "should_finish": result.should_finish,
                    "message": result.message,
                    "requires_confirmation": result.requires_confirmation,
                },
            }
        )

        # Add assistant response to context
        self._context.append(
            MessageBuilder.create_assistant_message(
                f"<think>{response.thinking}</think><answer>{response.action}</answer>"
            )
        )

        # Check if finished
        finished = action.get("_metadata") == "finish" or result.should_finish

        if finished and self.agent_config.verbose:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "🎉 " + "=" * 48)
            print(
                f"✅ {msgs['task_completed']}: {result.message or action.get('message', msgs['done'])}"
            )
            print("=" * 50 + "\n")

        return StepResult(
            success=result.success,
            finished=finished,
            action=action,
            thinking=response.thinking,
            message=result.message or action.get("message"),
        )

    @property
    def context(self) -> list[dict[str, Any]]:
        """Get the current conversation context."""
        return self._context.copy()

    @property
    def step_count(self) -> int:
        """Get the current step count."""
        return self._step_count
