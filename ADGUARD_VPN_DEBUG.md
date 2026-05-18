# AdGuard VPN на маке — диагностика и решение

## Симптомы

- AdGuard VPN не подключается, не видит список серверов, пишет «Сервер не отвечает».
- В debug-инфо: `VPN token: <пусто>`, `auth token: <пусто>`, `license status: —`.
- Логин в приложение не проходит, «ничего не происходит».
- При этом обычный интернет работает: `ping 1.1.1.1` идёт, `curl https://adguard-vpn.com` отдаёт TLS handshake.
- Telegram работает, YouTube и часть сайтов поначалу не открывались — это была отдельная проблема с локальным DNS-прокси AdGuard (блокировщика, не VPN). После выключения AdGuard-блокировщика YouTube заработал.

## Диагноз

В логах AdGuard VPN (`~/Library/Group Containers/TC3Q7MAJXF.com.adguard.mac/...AdGuard VPN-group.log`)
сотни ошибок одного типа:

```
E: (UTUNClient: init(onInited:)) UDP_SOCKET udp_socket_create_inner:
  [http3-conn-XXX] [id=N/[2a06:98c1:3120::3]:443]
  Failed to set socket destination: No route to host (65)
```

`2a06:98c1:3120::3` и `2a06:98c1:3121::3` — это **IPv6** адреса бэкенда AdGuard.
Приложение ходит к своим серверам по HTTP/3 (QUIC через UDP), резолвит AAAA-записи и
пытается стучаться по IPv6. IPv6 у пользователя сломан → backend недоступен →
нет токенов и списка локаций → логин не проходит.

При этом главный сайт `adguard-vpn.com` через `curl` работает, потому что отдаёт
IPv4-адрес Cloudflare (`104.18.x.x`) и `curl` выбирает IPv4 fallback.

## Корень проблемы

В таблице маршрутов IPv6 (`netstat -rn -f inet6`):

```
default → fe80::%utun0
default → fe80::%utun2
default → fe80::%utun3
default → fe80::%utun4
default → fe80::%utun5
default → fe80::%utun6
```

**Шесть utun-туннелей**, каждый держит default-маршрут для IPv6. Через `en0` (Wi-Fi)
default-маршрута для IPv6 нет вообще. То есть весь IPv6-трафик уходит в первый
попавшийся utun (вероятно, Tailscale на utun0), на той стороне нет маршрута до
адресов AdGuard — отсюда `No route to host`.

utun-интерфейсы накопились от:
- Tailscale (активный)
- AdGuard VPN system extension
- Возможно остатки других VPN после рестартов / переустановок

`networksetup -setv6off "Wi-Fi"` не помогает: проблема не в Wi-Fi, а в висящих utun.

## Решение

### Главное (must do)

1. **Полностью завершить Tailscale** (Quit, не Disconnect).
   - Иконка Tailscale в menu bar → **Quit Tailscale**.
   - Если иконки нет: Activity Monitor → найти `tailscale` / `Tailscale` → Force Quit.

2. **Полностью завершить AdGuard VPN** (Cmd+Q).

3. **Перезагрузить мак.** Без рестарта macOS не отдаёт висящие utun-интерфейсы.

4. После перезагрузки **НЕ запускать Tailscale**.

5. Проверить маршруты в терминале:
   ```
   netstat -rn -f inet6 | head -10
   ```
   Должно быть максимум 1–2 utun, и желательно либо без default через utun,
   либо default через `en0`.

6. Запустить AdGuard VPN. Логин и подключение должны пройти.

### Если после перезагрузки utun-ы возвращаются

Tailscale стартует автоматически:
- System Settings → General → Login Items & Extensions → **Login Items**.
- Убрать Tailscale из автозапуска.
- В самом Tailscale: Settings → отключить «Run at startup».

### Опциональная страховка — отключить IPv6 на Wi-Fi

Если IPv6 у провайдера всё равно битый (например, выдаётся через RA, но не маршрутизируется
наружу), можно его выключить совсем:

```
networksetup -listallnetworkservices
sudo networksetup -setv6off "Wi-Fi"
```

Вернуть обратно:
```
sudo networksetup -setv6automatic "Wi-Fi"
```

### Опциональная страховка — почистить локальный DNS

Если параллельно был установлен AdGuard-блокировщик (десктоп, не VPN), он мог
оставить resolver `127.0.0.1` и блокировать YouTube CDN. Лечилось выключением/удалением.
После любых сетевых изменений полезно сбросить DNS-кеш:

```
sudo dscacheutil -flushcache
sudo killall -HUP mDNSResponder
```

## Полезные диагностические команды

```bash
# Что в DNS
scutil --dns | head -50

# Резолвинг через внешний DNS
nslookup youtube.com 1.1.1.1

# IPv6 маршруты
netstat -rn -f inet6 | head -20

# Список активных utun
ifconfig | grep -E "^utun"

# Сетевые интерфейсы
networksetup -listallnetworkservices

# Проверка связи (IPv4 / IPv6)
ping  -c 3 1.1.1.1
ping6 -c 3 2606:4700:4700::1111

# TLS до AdGuard по IPv4 принудительно
curl -v -4 https://adguard-vpn.com 2>&1 | head -20
```

## Что в логах AdGuard VPN искать

Файлы логов:
```
~/Library/Group Containers/TC3Q7MAJXF.com.adguard.mac/Library/Application Support/com.adguard.mac.vpn/log/
```

Ключевые маркеры IPv6-проблемы:
- `UDP_SOCKET ... [id=.../[2a06:98c1:...]:443] Failed to set socket destination: No route to host (65)`
- `AuthenticationService ... Error Domain=VpnBackendErrorDomain Code=6 "Socket error"`
- `LocationsService ... waiting for vpn token`
- `BackendConfigFetcher get_config: Backend config was not fetched, continue with cached or default`

Если эти строки исчезают после перезагрузки (без Tailscale) — проблема решена.
