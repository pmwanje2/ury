import { connectQz } from "../../../../ury/public/js/qz-signing.js";
import { loadQz } from "../../../../packages/core/src/print/qz-runtime";
import { runSignedQz } from "../../../../packages/core/src/print/qz-signing";
import frappe from "../frappeSdk.js";

export async function loadQzPrinter(host){
    const qz = await loadQz();
    await connectQz(qz, async () => {
        const { message } = await frappe.call().get("ury.ury.api.ury_print.qz_certificate");
        return message;
    }, host);
    return "success";
}

export function disconnectQzPrinter(){
    const qz = window.qz;
    if(qz?.websocket.isActive())
        qz.websocket.disconnect();
}

export async function printWithQz(host, htmlToPrint, queue) {
    const qz = await loadQz();
    return runSignedQz(qz, (method, args) => frappe.call().post(method, args), async () => {
        if (!qz.websocket.isActive()) await loadQzPrinter(host);
        const printer = queue?.trim() || await qz.printers.getDefault();
        const data = [{ type: "html", format: "plain", data: htmlToPrint }];
        await qz.print(qz.configs.create(printer), data);
        return "printed";
    });
}
