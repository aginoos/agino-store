# SPDX-License-Identifier: GPL-3.0-or-later
"""Defaults of the AI mode of the build runner (docs/sandbox.md section 10.7).

Everything here can be overridden in the [builder] section (config.py): the variable names, the TOML keys and the
command lines of the two CLIs were taken from the design contract (ai-intake.md section 10.3) and have NOT been
verified against the pinned CLI versions yet (Claude Code 2.1.318, Codex rust-v0.166.0). Nothing in this file is a
secret: the API key values are placeholders, the real key lives only in the model proxy on the host.
"""

BRIDGE = "10.231.0.1"
PLACEHOLDER_KEY = "agino-placeholder"
PLACEHOLDER_MODEL = "agino"

# Environment of every AI process (and /etc/environment): the white-listing forward proxy for downloads.
DEFAULT_ENV = ("HTTPS_PROXY=http://%(b)s:3128 HTTP_PROXY=http://%(b)s:3128 "
               "https_proxy=http://%(b)s:3128 http_proxy=http://%(b)s:3128 "
               "NO_PROXY=%(b)s,localhost,127.0.0.1 no_proxy=%(b)s,localhost,127.0.0.1") % {"b": BRIDGE}

# Claude Code. UNVERIFIED names: ANTHROPIC_SMALL_FAST_MODEL and the ANTHROPIC_DEFAULT_*_MODEL family both exist in
# some versions; setting all of them to the placeholder model name is harmless.
DEFAULT_CLAUDE_ENV = (
    "ANTHROPIC_BASE_URL=http://%(b)s:8490/anthropic ANTHROPIC_API_KEY=%(k)s ANTHROPIC_MODEL=%(m)s "
    "ANTHROPIC_SMALL_FAST_MODEL=%(m)s ANTHROPIC_DEFAULT_HAIKU_MODEL=%(m)s ANTHROPIC_DEFAULT_SONNET_MODEL=%(m)s "
    "ANTHROPIC_DEFAULT_OPUS_MODEL=%(m)s DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 "
    "DISABLE_TELEMETRY=1") % {"b": BRIDGE, "k": PLACEHOLDER_KEY, "m": PLACEHOLDER_MODEL}

# Codex CLI. Verified 2026-11-05 with codex-cli 0.166.0 (a full import of Ristretto): CODEX_HOME, OPENAI_API_KEY and
# the config.toml keys below work.
CODEX_HOME = "/home/builder/.codex"
DEFAULT_CODEX_ENV = "CODEX_HOME=%s OPENAI_API_KEY=%s" % (CODEX_HOME, PLACEHOLDER_KEY)
DEFAULT_CODEX_TOML = """model = "%(m)s"
model_provider = "agino"

[model_providers.agino]
name = "agino"
base_url = "http://%(b)s:8490/openai/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
""" % {"b": BRIDGE, "m": PLACEHOLDER_MODEL}

# Shell snippets run by `sh -c` as `builder` in /work, wrapped in `timeout`. The prompt is the task file.
# UNVERIFIED flags: --bare, --permission-mode bypassPermissions, --dangerously-bypass-approvals-and-sandbox, --json.
DEFAULT_CLAUDE_CMD = ('claude --bare -p "$(cat /work/TASK.md)" --output-format stream-json --verbose '
                      '--permission-mode bypassPermissions')
DEFAULT_CODEX_CMD = ('codex exec --json --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check '
                     '"$(cat /work/TASK.md)"')
