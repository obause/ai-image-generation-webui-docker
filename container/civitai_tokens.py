"""Create InvokeAI's Civitai token setting without putting credentials in argv or files."""

import json
import os

CIVITAI_URL_REGEX = r"^https://civitai\.(?:com|red)/"


def remote_api_tokens(token: str) -> str:
    return json.dumps([{"url_regex": CIVITAI_URL_REGEX, "token": token}])


if __name__ == "__main__":
    print(remote_api_tokens(os.environ["CIVITAI_API_TOKEN"]))
