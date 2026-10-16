# U2 HTTP/1.1 capture at 212141e5

Local synthetic capture only. Chrome 150, Vite, and a loopback fixture served nine menu items over HTTP/1.1. Product reads were held open. This is not a live site and not staff acceptance.

Captured against code `212141e5a93f45c16b110b21aa6bb7e79b145e14`, before this evidence commit. Images from `6f816e27` and `94ac64ea` are older and are not proof of this head.

Each layout received the required order before the single optional add-on read. One guest Place Order was accepted while that add-on read was still held. The private staff note was not shown. The two order reads are the dev app's StrictMode.

| file | product reads held | order reads | guest action while held | menu | loading |
| --- | --- | --- | --- | --- | --- |
| mobile.png | 1 | 2 | 1 | visible | no |
| tablet.png | 1 | 2 | 1 | visible | no |
| landscape.png | 1 | 2 | 1 | visible | no |
| portrait.png | 1 | 2 | 1 | visible | no |
| qr.png | 1 | 2 | 1 | visible | no |

Browser response protocol for the menu read: `http/1.1`.
