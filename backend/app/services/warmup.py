"""Load the model into memory ahead of the first question.

On a CPU the first request after startup (or after the model was unloaded) can spend many seconds just loading
several GB of weights and reading the system prompt. A one-token request with the same system prompt and options
does that work in the background so the first real question starts quickly. Nothing is downloaded: if the model is
not installed this simply does nothing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.providers.base import ChatMessage, GenerationOptions, ProviderError
from app.services.chat import ASSISTANT_SYSTEM
from app.services.preferences import Preferences, model_for

log = logging.getLogger(__name__)


async def warm_up(llm: Any, prefs: Preferences) -> dict[str, Any]:
    model = model_for(prefs, "fast")
    g = prefs.generation
    opts = GenerationOptions(temperature=0, num_ctx=g.num_ctx, max_output_tokens=1, keep_alive=g.keep_alive)
    msgs = [ChatMessage("system", ASSISTANT_SYSTEM.format(name=prefs.assistant_name)), ChatMessage("user", "Hi")]
    t0 = time.time()
    try:
        await llm.complete(model, msgs, opts, purpose="warmup")
    except ProviderError as e:
        log.info("model warm-up skipped: %s", e)
        return {"ok": False, "model": model, "error": str(e)}
    except Exception as e:  # never let warm-up break startup
        log.info("model warm-up failed: %s", e)
        return {"ok": False, "model": model, "error": str(e)}
    took = round(time.time() - t0, 2)
    log.info("model %s warmed up in %.1fs", model, took)
    return {"ok": True, "model": model, "seconds": took}
