#!/usr/bin/env bash
# FunPayFlow: one-time VPS setup for automatic updates (docs/VPS_AUTO_UPDATE.md).
#
# Run ONCE by the owner on the server, from a downloaded copy (not piped):
#   curl -fsSLo /tmp/fpf-bootstrap.sh \
#     https://raw.githubusercontent.com/Vetal12350/FunPayFlow/<COMMIT>/scripts/vps_bootstrap.sh
#   sudo bash /tmp/fpf-bootstrap.sh --ref <COMMIT>
#
# What it does (idempotent, never restarts/stops the bot, never reads .env):
#  1. checks: one funpayflow.service, legacy funpay-bot.service not active,
#     Python 3.13 of the running bot, x86_64;
#  2. user funpayflow-deploy (no password, no shell access: forced command);
#  3. installs the deploy agent from the SAME commit, sha256-verified;
#  4. /etc/funpayflow-deploy/config.json and a narrow sudoers rule;
#  5. releases/<current sha> pointing at the CURRENT code, so even the first
#     update has an automatic rollback target;
#  6. a last drop-in 99-funpayflow-autoupdate.conf sets ExecStart to
#     /opt/funpayflow/current/venv/bin/python (the unit file and any older
#     drop-ins stay untouched) + daemon-reload ONLY: the running bot is not touched;
#  7. a dedicated SSH key for GitHub Actions; prints the 4 GitHub secrets.
set -euo pipefail
umask 077

PREFIX="${FPF_PREFIX:-}"            # tests only: run inside a scratch root
REPO_RAW="https://raw.githubusercontent.com/Vetal12350/FunPayFlow"
SERVICE="funpayflow.service"
LEGACY="funpay-bot.service"
DEPLOY_USER="funpayflow-deploy"
AGENT="$PREFIX/usr/local/sbin/funpayflow-deploy-agent"
ETC="$PREFIX/etc/funpayflow-deploy"
STATE="$PREFIX/var/lib/funpayflow-deploy"
OPT="$PREFIX/opt/funpayflow"
BACKUPS="$PREFIX/var/backups/funpayflow"
SUDOERS="$PREFIX/etc/sudoers.d/funpayflow-deploy"
HOME_DIR="$PREFIX/home/$DEPLOY_USER"
REF=""
HOST=""
ASSUME_YES=0

die() { echo "ОШИБКА: $*" >&2; echo "Ничего не перезапускалось. Исправьте и запустите снова." >&2; exit 1; }
say() { echo "==> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    *) die "неизвестный аргумент: $1" ;;
  esac
done
[[ "$REF" =~ ^[0-9a-f]{40}$ ]] || die "нужен --ref <40-символьный commit>"
if [ -z "$PREFIX" ] && [ "$(id -u)" != 0 ]; then die "запускайте через sudo"; fi

# ---------------------------------------------------------------- 1. checks
say "Проверка сервера"
[ "$(uname -m)" = "x86_64" ] || die "ожидалась архитектура x86_64"
[ "$(systemctl show -p LoadState --value "$SERVICE")" = "loaded" ] || die "$SERVICE не найден"
legacy_state="$(systemctl is-active "$LEGACY" 2>/dev/null || true)"
[ "$legacy_state" != "active" ] || die "старая служба $LEGACY активна: два бота одновременно запрещены"
UNIT="$PREFIX$(systemctl show -p FragmentPath --value "$SERVICE")"
[ -f "$UNIT" ] || die "файл юнита не найден"
DROPIN_DIR="$UNIT.d"
OUR_DROPIN="$DROPIN_DIR/99-funpayflow-autoupdate.conf"
data_dir="$(systemctl show -p Environment --value "$SERVICE" | tr ' ' '\n' \
            | sed -n 's/^FUNPAY_BOT_DATA_DIR=//p' | tr -d '"' | head -n1)"
[[ "$data_dir" == /* ]] || die "в юните не задан абсолютный FUNPAY_BOT_DATA_DIR"
[ -f "$PREFIX$data_dir/state.sqlite3" ] || die "в $data_dir нет state.sqlite3"

# The EFFECTIVE command line (unit + all drop-ins), as systemd runs it.
exec_line="$(systemctl show -p ExecStart --value "$SERVICE")"
old_python="$(sed -nE 's/.*argv\[\]=([^ ]+) -m funpayflow\.main ;.*/\1/p' <<<"$exec_line")"
[ -n "$old_python" ] && [ "$(grep -o 'argv\[\]=' <<<"$exec_line" | wc -l)" = 1 ] \
  || die "фактический ExecStart не вида '<python> -m funpayflow.main'"
CURRENT_PY="/opt/funpayflow/current/venv/bin/python"
if [ "$old_python" = "$CURRENT_PY" ]; then
  say "Юнит уже переведён на $CURRENT_PY"
  old_venv="$(readlink -f "$OPT/current/venv")"
else
  old_venv="$(dirname "$(dirname "$PREFIX$old_python")")"
fi
[ -f "$old_venv/pyvenv.cfg" ] || die "не найден venv текущего бота"
base_py="$(readlink -f "$old_venv/bin/python")"
"$base_py" -I -c 'import sys; assert sys.version_info[:2] == (3, 13)' \
  || die "бот работает не на Python 3.13"
"$base_py" -I -c 'import venv, ensurepip' || die "в Python 3.13 нет модулей venv/ensurepip"
old_code="$(systemctl show -p WorkingDirectory --value "$SERVICE")"
old_sha="$(git -C "$PREFIX$old_code" rev-parse HEAD 2>/dev/null || true)"
[[ "$old_sha" =~ ^[0-9a-f]{40}$ ]] || old_sha="0000000000000000000000000000000000000000"
if [ -z "$HOST" ]; then HOST="$(hostname -I | awk '{print $1}')"; fi
[ -n "$HOST" ] || die "не удалось определить адрес сервера, укажите --host"
say "Данные: $data_dir · текущий код: ${old_sha:0:12} · Python: $base_py · адрес: $HOST"

if [ "$ASSUME_YES" != 1 ]; then
  read -r -p "Продолжить настройку? Бот НЕ будет перезапущен. [y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || die "отменено"
fi

# ----------------------------------------------------------- 2. deploy user
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  say "Создаю пользователя $DEPLOY_USER"
  useradd --create-home --shell /bin/bash "$DEPLOY_USER"
fi
install -d -m 0700 "$HOME_DIR/.ssh"

# ------------------------------------------------------------- 3. the agent
say "Устанавливаю агент из коммита ${REF:0:12}"
tmp_agent="$(mktemp)"
curl -fsSL "$REPO_RAW/$REF/scripts/deploy_agent.py" -o "$tmp_agent"
sed -i "1s|.*|#!$base_py -I|" "$tmp_agent"
"$base_py" -I -m py_compile "$tmp_agent" || die "агент не компилируется"
install -D -m 0755 "$tmp_agent" "$AGENT"
rm -f "$tmp_agent"

# ------------------------------------------------- 4. config and sudo rule
install -d -m 0700 "$ETC" "$STATE" "$OPT/releases" "$BACKUPS"
cat > "$ETC/config.json" <<EOF
{"root": "/var/lib/funpayflow-deploy", "releases": "/opt/funpayflow/releases",
 "current": "/opt/funpayflow/current", "data_dir": "$data_dir",
 "backups": "/var/backups/funpayflow", "service": "$SERVICE",
 "forbidden_services": ["$LEGACY"], "python": "$base_py",
 "keep_releases": 3, "health_timeout": 180, "health_stable": 20,
 "require_heartbeat": true, "min_free_mb": 1024}
EOF
chmod 0600 "$ETC/config.json"
"$base_py" -I - "$ETC/config.json" "$AGENT" <<'PY' || die "конфиг не прошёл проверку агента"
import importlib.machinery, importlib.util, json, sys
loader = importlib.machinery.SourceFileLoader("agent", sys.argv[2])
spec = importlib.util.spec_from_loader("agent", loader)
agent = importlib.util.module_from_spec(spec); loader.exec_module(agent)
agent.validate_config(json.load(open(sys.argv[1])))
PY
sudo_tmp="$(mktemp)"
cat > "$sudo_tmp" <<EOF
Defaults!/usr/local/sbin/funpayflow-deploy-agent env_keep += "SSH_ORIGINAL_COMMAND"
$DEPLOY_USER ALL=(root) NOPASSWD: /usr/local/sbin/funpayflow-deploy-agent ""
EOF
visudo -cf "$sudo_tmp" >/dev/null || die "правило sudo не прошло visudo"
install -D -m 0440 "$sudo_tmp" "$SUDOERS"
rm -f "$sudo_tmp"

# ------------------------------- 5. current code as the first release
release="$OPT/releases/$old_sha"
if [ ! -e "$OPT/current" ]; then
  say "Регистрирую текущую версию как релиз ${old_sha:0:12} (цель для отката)"
  install -d -m 0755 "$release"
  ln -sfn "$old_venv" "$release/venv"
  ln -sfn "$OPT/releases/$old_sha" "$OPT/current"
fi
"$OPT/current/venv/bin/python" -I -c \
  'import importlib.util, sys; sys.exit(importlib.util.find_spec("funpayflow") is None)' \
  || die "через /opt/funpayflow/current бот не находится"

# ------------------------------------- 6. unit -> current (no restart)
if [ "$old_python" != "$CURRENT_PY" ]; then
  say "Добавляю $OUR_DROPIN: ExecStart -> $CURRENT_PY (без перезапуска)"
  install -d -m 0755 "$DROPIN_DIR"
  printf '[Service]\nExecStart=\nExecStart=%s -m funpayflow.main\n' "$CURRENT_PY" > "$OUR_DROPIN"
  chmod 0644 "$OUR_DROPIN"
  systemctl daemon-reload
  if ! systemctl show -p ExecStart --value "$SERVICE" | grep -qF "argv[]=$CURRENT_PY -m funpayflow.main"; then
    rm -f "$OUR_DROPIN"
    systemctl daemon-reload
    die "systemd не применил $OUR_DROPIN (другой drop-in важнее?) — файл удалён"
  fi
fi

# --------------------------------------------------------- 7. SSH key
key="$ETC/github-actions-key"
if [ ! -f "$HOME_DIR/.ssh/authorized_keys" ] || ! grep -q funpayflow-deploy-agent "$HOME_DIR/.ssh/authorized_keys"; then
  say "Создаю отдельный SSH-ключ для GitHub Actions"
  rm -f "$key" "$key.pub"
  ssh-keygen -q -t ed25519 -N "" -C "funpayflow-github-actions" -f "$key"
  printf 'restrict,command="sudo -n /usr/local/sbin/funpayflow-deploy-agent" %s\n' \
    "$(cat "$key.pub")" > "$HOME_DIR/.ssh/authorized_keys"
  chmod 0600 "$HOME_DIR/.ssh/authorized_keys"
  if [ -z "$PREFIX" ]; then chown -R "$DEPLOY_USER:$DEPLOY_USER" "$HOME_DIR/.ssh"; fi
fi
if sshd -T 2>/dev/null | grep -qiE '^allowusers ' && ! sshd -T 2>/dev/null | grep -iE '^allowusers ' | grep -qw "$DEPLOY_USER"; then
  echo "ВНИМАНИЕ: в sshd задан AllowUsers без $DEPLOY_USER — добавьте его вручную." >&2
fi

# ----------------------------------------------------------- 8. secrets
echo
echo "================ ГОТОВО. Бот продолжает работать как раньше. ================"
echo "GitHub → Settings → Environments → production → Environment secrets:"
echo
echo "DEPLOY_HOST:  $HOST"
echo "DEPLOY_USER:  $DEPLOY_USER"
echo "DEPLOY_KNOWN_HOSTS:"
echo "$HOST $(cut -d' ' -f1,2 "$PREFIX/etc/ssh/ssh_host_ed25519_key.pub")"
if [ -f "$key" ]; then
  echo "DEPLOY_SSH_KEY (всё, включая строки BEGIN/END):"
  cat "$key"
  echo
  echo "После копирования удалите ключ с сервера:  sudo rm -f $key $key.pub"
fi
echo
echo "Откат настройки: sudo rm -f ${OUR_DROPIN#$PREFIX} && sudo systemctl daemon-reload"
