import json
import requests

from . import config as cfg
from . import db


def _ollama_perf_kwargs(config):
    """Build the extra request fields that control speed/resource usage
    for an Ollama generate call, based on user-configurable settings."""
    kwargs = {
        "options": {
            "num_ctx": int(config.get("ollama_num_ctx", 2048)),
            "num_predict": int(config.get("ollama_num_predict", 200)),
        },
        "keep_alive": config.get("ollama_keep_alive", "5m"),
    }
    # "think" is only honored by reasoning-capable models (e.g. qwen3);
    # it's harmless to send for models that don't support it.
    if not config.get("ollama_think", False):
        kwargs["think"] = False
    return kwargs


def _format_examples(examples):
    """Render a list of category examples (from db.get_category_examples)
    as a few-shot block for the prompt. Returns '' if there are none."""
    if not examples:
        return ""
    lines = ["Here are some examples to guide your decision:\n"]
    for ex in examples:
        tags = ", ".join(ex.get("tags", []) or [])
        label_str = "MATCH" if ex.get("label") == "match" else "NOT A MATCH"
        lines.append(
            f"Example ({label_str}):\n"
            f"Title/Description: {ex.get('title', '')}\n"
            f"Author: {ex.get('author', '')}\n"
            f"Hashtags/Tags: {tags}\n"
        )
    return "\n".join(lines) + "\n"


def build_prompt(category_prompt, meta, examples=None):
    tags = ", ".join(meta.get("tags", []) or [])
    return (
        "You are a content filter for a TikTok video archiving tool.\n"
        "The user is looking for videos matching this description:\n"
        f'"{category_prompt}"\n\n'
        f"{_format_examples(examples)}"
        "Here is the metadata for a candidate video:\n"
        f"Title/Description: {meta.get('title', '')}\n"
        f"Author: {meta.get('author', '')}\n"
        f"Hashtags/Tags: {tags}\n\n"
        "Decide if this video matches what the user is looking for.\n"
        "Respond with ONLY a JSON object, no other text, in this exact format:\n"
        '{"match": true or false, "reason": "short explanation"}'
    )


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # Find first { ... } block in case the model added extra text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def build_batch_prompt(category_prompt, metas, examples=None):
    items = []
    for i, meta in enumerate(metas):
        tags = ", ".join(meta.get("tags", []) or [])
        items.append(
            f"Video {i}:\n"
            f"Title/Description: {meta.get('title', '')}\n"
            f"Author: {meta.get('author', '')}\n"
            f"Hashtags/Tags: {tags}\n"
        )
    joined = "\n".join(items)
    return (
        "You are a content filter for a TikTok video archiving tool.\n"
        "The user is looking for videos matching this description:\n"
        f'"{category_prompt}"\n\n'
        f"{_format_examples(examples)}"
        "Here is metadata for several candidate videos:\n\n"
        f"{joined}\n"
        "For EACH video, decide if it matches what the user is looking for.\n"
        "Respond with ONLY a JSON array, no other text, with one object per video "
        "in the same order, in this exact format:\n"
        '[{"match": true or false, "reason": "short explanation"}, ...]'
    )


def _extract_json_array(text, expected_len):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array")
    return data


class LLMUnreachable(Exception):
    pass


def evaluate_videos_batch(category_prompt, metas, chunk_size=10, examples=None):
    """Evaluate multiple videos in one or more LLM calls (chunked to keep
    prompts small). Falls back to per-video evaluation on parse failure.
    Returns None if the LLM backend is unreachable, so the caller can skip
    this scan entirely and retry these videos next time, rather than
    permanently marking them 'rejected'."""
    if not metas:
        return []
    results = []
    for i in range(0, len(metas), chunk_size):
        try:
            results.extend(_evaluate_chunk(category_prompt, metas[i:i + chunk_size], examples))
        except LLMUnreachable:
            return None
    return results


def _evaluate_chunk(category_prompt, metas, examples=None):
    if len(metas) == 1:
        result = evaluate_video(category_prompt, metas[0], examples)
        if result[1].startswith("LLM error:") and "unreachable" in result[1]:
            raise LLMUnreachable(result[1])
        return [result]

    config = cfg.load_config()
    prompt = build_batch_prompt(category_prompt, metas, examples)

    try:
        if config["llm_provider"] == "ollama":
            resp = requests.post(
                f"{config['ollama_url']}/api/generate",
                json={
                    "model": config["ollama_model"],
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    **_ollama_perf_kwargs(config),
                },
                timeout=300,
            )
            resp.raise_for_status()
            text = resp.json().get("response", "")
        else:
            headers = {
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            }
            resp = requests.post(
                f"{config['api_base_url']}/chat/completions",
                headers=headers,
                json={
                    "model": config["api_model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": int(config.get("ollama_num_predict", 200)),
                },
                timeout=300,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]

        data = _extract_json_array(text, len(metas))
        if len(data) != len(metas):
            raise ValueError(f"Expected {len(metas)} results, got {len(data)}")

        results = []
        for item in data:
            results.append((bool(item.get("match", False)), str(item.get("reason", ""))))
        return results
    except Exception as e:
        msg = _describe_request_error(e, config)
        if isinstance(e, requests.exceptions.ConnectionError):
            db.log("ERROR", f"Batch LLM evaluation failed: {msg}; skipping this scan")
            raise LLMUnreachable(msg)
        db.log("WARN", f"Batch LLM evaluation failed ({msg}); falling back to per-video calls")
        return [evaluate_video(category_prompt, m, examples) for m in metas]


def _describe_request_error(e, config):
    """Turn a requests exception into a clearer message, distinguishing
    'can't reach the server at all' from 'server responded but with an error'."""
    if isinstance(e, requests.exceptions.ConnectionError):
        if config.get("llm_provider") == "ollama":
            return f"Ollama unreachable at {config.get('ollama_url')} ({e})"
        return f"API unreachable at {config.get('api_base_url')} ({e})"
    if isinstance(e, requests.exceptions.Timeout):
        return f"LLM request timed out ({e})"
    if isinstance(e, requests.exceptions.HTTPError):
        return f"LLM server returned an error response ({e})"
    return str(e)


def test_connection():
    """Check connectivity to the configured LLM backend and, for Ollama,
    return the list of available models. Used by the 'Test connection'
    button in Settings."""
    config = cfg.load_config()
    if config["llm_provider"] == "ollama":
        try:
            r = requests.get(f"{config['ollama_url']}/api/tags", timeout=10)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            configured = config.get("ollama_model")
            return {
                "ok": True,
                "provider": "ollama",
                "url": config["ollama_url"],
                "models": models,
                "configured_model_available": configured in models,
                "configured_model": configured,
            }
        except Exception as e:
            return {
                "ok": False,
                "provider": "ollama",
                "url": config["ollama_url"],
                "error": _describe_request_error(e, config),
            }
    else:
        try:
            headers = {"Authorization": f"Bearer {config['api_key']}"}
            r = requests.get(f"{config['api_base_url']}/models", headers=headers, timeout=10)
            r.raise_for_status()
            return {"ok": True, "provider": "api", "url": config["api_base_url"]}
        except Exception as e:
            return {
                "ok": False,
                "provider": "api",
                "url": config["api_base_url"],
                "error": _describe_request_error(e, config),
            }


def evaluate_video(category_prompt, meta, examples=None):
    config = cfg.load_config()
    prompt = build_prompt(category_prompt, meta, examples)

    try:
        if config["llm_provider"] == "ollama":
            resp = requests.post(
                f"{config['ollama_url']}/api/generate",
                json={
                    "model": config["ollama_model"],
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    **_ollama_perf_kwargs(config),
                },
                timeout=180,
            )
            resp.raise_for_status()
            text = resp.json().get("response", "")
        else:
            headers = {
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            }
            resp = requests.post(
                f"{config['api_base_url']}/chat/completions",
                headers=headers,
                json={
                    "model": config["api_model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": int(config.get("ollama_num_predict", 200)),
                },
                timeout=180,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]

        data = _extract_json(text)
        return bool(data.get("match", False)), str(data.get("reason", ""))
    except Exception as e:
        msg = _describe_request_error(e, config)
        db.log("ERROR", f"LLM evaluation failed: {msg}")
        return False, f"LLM error: {msg}"
