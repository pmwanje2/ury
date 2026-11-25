import axios from "axios";
import { loadQz } from "../../../../packages/core/src/print/qz-runtime";
import { runSignedQz } from "../../../../packages/core/src/print/qz-signing";
import frappe from "../frappeSdk.js";

export async function loadQzPrinter(host){
    const qz = await loadQz();
    if (qz.websocket.isActive()) return "success";
    return new Promise((resolve,reject)=>{
        qz.security.setCertificatePromise((resolve)=>{
            axios.get("/assets/ury/files/cert.pem")
            .then((response)=>{
                resolve(response.data)  
            }).catch((err)=>{
                reject({
                    custom:true,
                    title:"Error during fetching certificate",
                    message:err
                });
            })
        });
        if(!qz.websocket.isActive()){
            qz.websocket.connect({
                host,
                usingSecure:false
            })
            .then(()=>resolve("success"))
            .catch((err)=>{
                reject({
                    custom:true,
                    title:"Error during connection to printer",
                    message:String(err)
                });
            })
        }
    });
}

export function disconnectQzPrinter(){
    const qz = window.qz;
    if(qz?.websocket.isActive())
        qz.websocket.disconnect();
}

export async function printWithQz(host, htmlToPrint) {
    const qz = await loadQz();
    return runSignedQz(qz, (method, args) => frappe.call().post(method, args), async () => {
        if (!qz.websocket.isActive()) await loadQzPrinter(host);
        const printer = await qz.printers.getDefault();
        const data = [{ type: "html", format: "plain", data: htmlToPrint }];
        await qz.print(qz.configs.create(printer), data);
        return "printed";
    });
}
