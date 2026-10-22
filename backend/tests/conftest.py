"""Keep the test suite offline on every machine.

Importing the API (backend.apis.main) loads backend/.env, which on a developer machine holds real
LLM keys; a test that builds an agent would then call Groq live and flake on rate limits or wording.
load_env_file never overrides a variable that is already set, so blanking the keys here, before any
test module is collected, keeps them out. Set CRICBOT_LIVE_TESTS=1 to run against the real providers.
"""

import os

if os.getenv("CRICBOT_LIVE_TESTS") != "1":
    for key in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MAC_SERVING_API_KEY"):
        os.environ[key] = ""
