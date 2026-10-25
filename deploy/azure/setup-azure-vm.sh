#!/usr/bin/env bash
# avdlab on a fresh Ubuntu 24.04 VM (Azure or anywhere) with Caddy + Let's Encrypt.  Idempotent: safe to re-run.
#
#   sudo bash setup-azure-vm.sh <repo-url | tarball-url | tarball-path> <domain> <acme-email> [admin-password]
#
#   1. installs Docker Engine + compose plugin from download.docker.com
#   2. puts the code in /opt/avdlab  (git clone / git pull, or unpacks the tarball over it)
#   3. creates /opt/avdlab/deploy/.env from .env.example (an existing .env is kept; only the keys below are set)
#   4. docker compose --profile public up -d --build
# Firewalling is done by the Azure NSG (22 from your IP, 80, 443); this script opens nothing.
# The DNS A record <domain> → VM public IP must exist before step 4, or the certificate request fails.
set -euo pipefail

SRC="${1:?usage: $0 <repo-url|tarball> <domain> <acme-email> [admin-password]}"
DOMAIN="${2:?domain missing}"
EMAIL="${3:?acme email missing}"
ADMIN_PW="${4:-}"
DIR=/opt/avdlab
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

# ---- 1. Docker (official apt repo)
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq ca-certificates curl git openssl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi
[ -n "${SUDO_USER:-}" ] && usermod -aG docker "$SUDO_USER" || true

# ---- 2. code
mkdir -p "$DIR"
case "$SRC" in
  *.tar.gz|*.tgz)
    if [[ "$SRC" =~ ^https?:// ]]; then curl -fsSL "$SRC" -o /tmp/avdlab.tgz; SRC=/tmp/avdlab.tgz; fi
    tar -xzf "$SRC" -C "$DIR" --strip-components=1 ;;          # release/GitHub tarballs have one top-level dir
  *)
    if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only
    else git clone "$SRC" "$DIR"; fi ;;
esac

# ---- 3. .env
cd "$DIR/deploy"
[ -f .env ] || cp .env.example .env
chmod 600 .env
get() { sed -n "s/^$1=//p" .env | tail -n1; }
set_kv() {  # replace an active or commented-out KEY=..., else append
  local v; v=$(printf '%s' "$2" | sed -e 's/[\\&|]/\\&/g')          # escape sed specials (\ & |)
  if grep -qE "^#? ?$1=" .env; then sed -i -E "s|^#? ?$1=.*|$1=$v|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}
GENERATED=()
set_kv AVDLAB_DOMAIN "$DOMAIN"
set_kv AVDLAB_ACME_EMAIL "$EMAIL"
set_kv AVDLAB_PUBLIC_URL "https://$DOMAIN"
set_kv AVDLAB_TRUST_PROXY_HEADERS true
set_kv AVDLAB_BIND_ADDRESS 127.0.0.1
if [ -n "$ADMIN_PW" ]; then
  set_kv AVDLAB_ADMIN_PASSWORD "$ADMIN_PW"
elif [ "$(get AVDLAB_ADMIN_PASSWORD)" = "change-me" ] || [ -z "$(get AVDLAB_ADMIN_PASSWORD)" ]; then
  ADMIN_PW="Av-$(openssl rand -hex 6)-$(openssl rand -base64 12 | tr -dc 'A-Za-z0-9' | head -c 10)"
  set_kv AVDLAB_ADMIN_PASSWORD "$ADMIN_PW"; GENERATED+=("AVDLAB_ADMIN_PASSWORD=$ADMIN_PW")
fi
case "$(get AVDLAB_SESSION_SECRET)" in
  ""|change-me-session-secret)
    S=$(openssl rand -hex 32); set_kv AVDLAB_SESSION_SECRET "$S"; GENERATED+=("AVDLAB_SESSION_SECRET=$S") ;;
esac

# ---- 4. start
docker compose --profile public up -d --build

echo
echo "avdlab: https://$DOMAIN   (certificate: docker compose -f $DIR/deploy/docker-compose.yml logs caddy)"
if [ ${#GENERATED[@]} -gt 0 ]; then
  echo "Generated now and stored in $DIR/deploy/.env (shown once — note the admin password):"
  printf '  %s\n' "${GENERATED[@]}"
fi
echo "The admin password only seeds the first account (users table empty); sign in as $(U=$(get AVDLAB_ADMIN_USER); echo "${U:-admin}")."
