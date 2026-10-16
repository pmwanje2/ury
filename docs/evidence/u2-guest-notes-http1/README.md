# U2 HTTP/1.1 capture

Local synthetic capture only. Chrome 150, Vite, and a loopback fixture. This is not a live site and not staff acceptance.

## Initial hydration at 212141e5

The five `mobile.png`, `tablet.png`, `landscape.png`, `portrait.png`, and `qr.png` images were captured against `212141e5a93f45c16b110b21aa6bb7e79b145e14`, before commit `5b38d63d`. Images from `6f816e27` and `94ac64ea` are older and are not proof of later heads.

Each layout received the required order before the single optional add-on read. One guest Place Order was accepted while that add-on read was still held. The private staff note was not shown. The two order reads are the dev app's StrictMode. These images do not exercise Retry add-ons.

| file | product reads held | order reads | guest action while held | menu | loading |
| --- | --- | --- | --- | --- | --- |
| mobile.png | 1 | 2 | 1 | visible | no |
| tablet.png | 1 | 2 | 1 | visible | no |
| landscape.png | 1 | 2 | 1 | visible | no |
| portrait.png | 1 | 2 | 1 | visible | no |
| qr.png | 1 | 2 | 1 | visible | no |

## Retry queue at 5a9a7799

The five `*-retry.png` images were captured against `5a9a7799b029a305b1f10c2271da3aee45583d27`, before the docs commit that adds them. Chrome 150.0.7871.186, Vite 6.4.3, loopback HTTP/1.1. Dish0-Dish4 failed once, Dish5 stayed open, then the five visible Retry add-ons controls were clicked. The fixture received no new product read for those clicks. Place Order for Dish8 reached the fixture while that one read stayed open. The accepted order is `[{ "item": "Dish8", "qty": 1 }]`, with no price and no comment. The raw staff note was not shown.

The loading labels in the images are queued retry feedback. They are not six sockets. `maxHeld` stayed 1. Device layouts also recorded duplicate fast 503s from dev StrictMode; those responses were not held.

| file | optional reads held after retries | product reads added by retries | POSTs while held | protocol |
| --- | --- | --- | --- | --- |
| mobile-retry.png | 1 | 0 | 1 | http/1.1 |
| tablet-retry.png | 1 | 0 | 1 | http/1.1 |
| landscape-retry.png | 1 | 0 | 1 | http/1.1 |
| portrait-retry.png | 1 | 0 | 1 | http/1.1 |
| qr-retry.png | 1 | 0 | 1 | http/1.1 |
