#!/usr/bin/env bash
# Run as the service account, not root. Only optional systemd integration uses sudo.
set -euo pipefail
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
default_code_dir=$script_dir
if [[ -f "$script_dir/../app/pyproject.toml" ]]; then
  default_code_dir=$(cd -- "$script_dir/../app" && pwd -P)
fi
code_dir=''
data_dir=''
install_service=0
while (($#)); do
  case "$1" in
    --code-dir) code_dir=${2:?Missing code directory}; shift 2 ;;
    --data-dir) data_dir=${2:?Missing data directory}; shift 2 ;;
    --systemd) install_service=1; shift ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

if [[ $(id -u) -eq 0 ]]; then
  printf 'Run as a regular user; sudo is used only for optional systemd setup.\n' >&2
  exit 1
fi
if [[ -z "$code_dir" ]]; then
  read -r -p "Code directory [$default_code_dir]: " code_dir || exit 2
  code_dir=${code_dir:-$default_code_dir}
fi
if [[ -z "$data_dir" ]]; then
  read -r -p "Private data directory [$HOME/.local/share/funpayflow]: " data_dir || exit 2
  data_dir=${data_dir:-$HOME/.local/share/funpayflow}
fi
[[ "$code_dir" = /* && "$data_dir" = /* ]] || {
  printf 'Both directories must be absolute paths.\n' >&2; exit 2;
}
code_dir=$(cd -- "$code_dir" && pwd -P)
[[ -f "$code_dir/pyproject.toml" && -f "$code_dir/uv.lock" && -f "$code_dir/setup_config.py" ]] || {
  printf 'Code directory does not contain the release files.\n' >&2; exit 1;
}
mkdir -p -- "$data_dir"
data_dir=$(cd -- "$data_dir" && pwd -P)
if [[ "$data_dir" == / || "$data_dir" == "$HOME" || "$data_dir" == "$code_dir" ||
      "$data_dir" == "$code_dir/"* ]]; then
  printf 'Choose a dedicated private data directory outside the code tree.\n' >&2
  exit 2
fi
chmod 700 -- "$data_dir"

if command -v uv >/dev/null 2>&1; then
  uv_exe=$(command -v uv)
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  uv_exe=$HOME/.local/bin/uv
else
  command -v curl >/dev/null 2>&1 || {
    printf 'Install curl or uv, then rerun install.sh.\n' >&2; exit 1;
  }
  bootstrap=$(mktemp)
  trap 'rm -f -- "$bootstrap"' EXIT
  # Official Astral uv installer, downloaded to a file for an explicit source.
  curl --fail --location --silent --show-error https://astral.sh/uv/install.sh -o "$bootstrap"
  sh "$bootstrap"
  uv_exe=$HOME/.local/bin/uv
  [[ -x "$uv_exe" ]] || { printf 'uv installation failed.\n' >&2; exit 1; }
fi

"$uv_exe" python install 3.13
(cd -- "$code_dir" && "$uv_exe" sync --locked --no-dev --python 3.13)
"$code_dir/.venv/bin/python" "$code_dir/setup_config.py" --data-dir "$data_dir"
chmod 600 -- "$data_dir/.env"

if ((install_service)); then
  command -v sudo >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1 || {
    printf 'systemd and sudo are required for --systemd. Setup is otherwise complete.\n' >&2; exit 1;
  }
  service_name=funpayflow.service
  if [[ -e "/etc/systemd/system/$service_name" ]]; then
    printf 'Existing service was preserved; inspect it manually before updating.\n' >&2
    exit 1
  fi
  "$code_dir/.venv/bin/python" "$code_dir/render_service.py" \
    --code-dir "$code_dir" --data-dir "$data_dir" \
    --output "$data_dir/$service_name"
  sudo install -m 644 -- "$data_dir/$service_name" "/etc/systemd/system/$service_name"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$service_name"
  printf 'Service installed. Use: sudo systemctl status %s\n' "$service_name"
else
  printf 'Setup complete. Start with: FUNPAY_BOT_DATA_DIR="%s" "%s/.venv/bin/python" "%s/main.py"\n' \
    "$data_dir" "$code_dir" "$code_dir"
  printf 'Optional service setup: bash "%s/install.sh" --code-dir "%s" --data-dir "%s" --systemd\n' \
    "$script_dir" "$code_dir" "$data_dir"
fi
