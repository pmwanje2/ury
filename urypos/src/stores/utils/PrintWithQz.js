import axios from "axios";
import { loadQz } from "../../../../packages/core/src/print/qz-runtime";
import { configureQzSignature } from "../../../../packages/core/src/print/qz-signing";
import frappe from "../frappeSdk.js";

export async function loadQzPrinter(host){
    const qz = await loadQz();
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

export async function printWithQz(host,htmlToPrint){
    const qz = await loadQz();
    return new Promise((resolve,reject)=>{
        configureQzSignature(qz, (method, args) => frappe.call().post(method, args));

        const printing=()=>{
            qz.printers.getDefault()
                .then(async (printer)=>{
                    const data=[{
                        type:"html",
                        format:"plain",
                        data:htmlToPrint
                    }];
                    const config=qz.configs.create(printer)
                    try {
                        await qz.print(config, data);
                        return resolve("printed");
                    } catch (e) {
                        qz.websocket.disconnect();
                        reject(
                            {
                                custom: true,
                                title: "Print failed",
                                message: String(e)
                            }
                        );
                    }
                })
                .catch((err)=>{
                    qz.websocket.disconnect();
                    reject({
                        custom:true,
                        title:"Error looking up for printer",
                        message:String(err)
                    })
            })
        }
        
        if(qz.websocket.isActive()){
            printing();
        }
        else{
            loadQzPrinter(host).then(()=>printing())
            .catch((err)=>reject(err))
        }
    })
}