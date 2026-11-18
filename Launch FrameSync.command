#!/bin/zsh
set -e
cd -- "${0:A:h}"
if [[ -x .venv/bin/framesync ]]; then
  exec .venv/bin/framesync
fi
FRAME_UV="$(command -v uv || true)"
if [[ -z "$FRAME_UV" && -x /opt/homebrew/bin/uv ]]; then
  FRAME_UV=/opt/homebrew/bin/uv
fi
if [[ -z "$FRAME_UV" ]]; then
  print 'FrameSync needs uv. Install it from https://docs.astral.sh/uv/getting-started/installation/ and reopen this launcher.'
  read '?Press Return to close.'
  exit 1
fi
exec "$FRAME_UV" run --locked --python 3.14 framesync
