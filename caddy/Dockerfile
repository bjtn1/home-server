from caddy:2-builder as builder
run xcaddy build --with github.com/caddy-dns/porkbun@v0.3.1

from caddy:2
copy --from=builder /usr/bin/caddy /usr/bin/caddy
