"""Check that background tasks can publish claude.ai artifacts: one cheap session on JARVIS's own Claude login, with
the options tasks get (CLAUDE_CODE_ARTIFACT=1 and the Artifact tool), asked to publish a tiny page. Not a unit test:
it spends a little usage and publishes a real page.

    uv run python -m jarvis.tasks.artifact_check                 # a "hello" page
    uv run python -m jarvis.tasks.artifact_check --file page.html # publish this page instead (copied first)

`make artifact-check` runs the first. It reports whether the CLI loaded the Artifact tool, how it's signed in (the
tool needs a claude.ai subscription login, not an API key), and the link that came back. It never prints tokens."""

import argparse
import asyncio
import re
import shutil
import sys
import tempfile
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, SystemMessage, ToolResultBlock, ToolUseBlock, UserMessage

from jarvis.brain.sdk import sdk_options
from jarvis.core.config import FAST_MODEL, TASK_CACHE_DIR
from jarvis.tasks.worker import ARTIFACT, ARTIFACT_ENV, result_text

HELLO = "<title>JARVIS hello</title>\n<style>body{padding:24px;background:#fafafa}</style>\n<h1>Hello from JARVIS</h1>\n"
LINK = re.compile(r"https://claude\.ai/(?:code/)?artifact/[\w-]+")


async def check(page: Path | None, model: str) -> int:
    TASK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="artifact-check-", dir=TASK_CACHE_DIR))
    target = folder / (page.name if page else "hello.html")
    if page:
        shutil.copy(page, target)
    else:
        target.write_text(HELLO)
    options = sdk_options(
        env=ARTIFACT_ENV, cwd=str(folder), model=model, tools=[ARTIFACT], allowed_tools=[ARTIFACT],
        permission_mode="default", max_turns=4,
        system_prompt="You publish files with the Artifact tool. The page is already written: publish it as it is, "
                      "without loading skills or changing it, then reply with only the link the tool returned.",
    )
    prompt = f'Publish {target} with the Artifact tool (action "publish", icon "page"), then reply with only its link.'
    called, results, link = False, [], ""
    async with ClaudeSDKClient(options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                print(f"CLI {msg.data.get('claude_code_version', '?')}, API key from: {msg.data.get('apiKeySource', '?')}"
                      " ('none' is right: the tool needs a claude.ai login, and a key would win over it)")
                print(f"Artifact tool loaded: {ARTIFACT in (msg.data.get('tools') or [])}")
            elif isinstance(msg, AssistantMessage):
                called |= any(isinstance(b, ToolUseBlock) and b.name == ARTIFACT for b in msg.content)
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                results += [(b.is_error, result_text(b.content)) for b in msg.content if isinstance(b, ToolResultBlock)]
            elif isinstance(msg, ResultMessage):
                for failed, text in results:
                    print(f"Artifact {'error' if failed else 'result'}: {text[:600]}")
                texts = [text for _, text in results] + [msg.result or ""]
                link = next((m.group() for text in texts if (m := LINK.search(text))), "")
                print(f"Reply: {(msg.result or msg.subtype)[:400]}")
                print(f"Cost: ${msg.total_cost_usd or 0:.4f}")
    print(f"Published: {link}" if link else
          f"Not published{'' if called else ': the Artifact tool was never called'}. Local copy: {target}")
    return 0 if link else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, help="an .html page to publish instead of the hello page")
    parser.add_argument("--model", default=FAST_MODEL)
    args = parser.parse_args()
    if args.file and not args.file.is_file():
        sys.exit(f"No such file: {args.file}")
    sys.exit(asyncio.run(check(args.file.resolve() if args.file else None, args.model)))


if __name__ == "__main__":
    main()
