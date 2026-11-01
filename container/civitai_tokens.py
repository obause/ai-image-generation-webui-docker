"""Create InvokeAI's remote tokens without putting credentials in argv or files."""

import json
import os

CIVITAI_URL_REGEX = r"^https://civitai\.(?:com|red)/"
HF_URL_REGEX = r"^https://huggingface\.co/"


def remote_api_tokens(civitai_token: str | None, hf_token: str | None = None) -> str:
    tokens = []
    if civitai_token:
        tokens.append({"url_regex": CIVITAI_URL_REGEX, "token": civitai_token})
    if hf_token:
        tokens.append({"url_regex": HF_URL_REGEX, "token": hf_token})
    return json.dumps(tokens)


if __name__ == "__main__":
    print(remote_api_tokens(os.environ.get("CIVITAI_API_TOKEN"), os.environ.get("HF_TOKEN")))
