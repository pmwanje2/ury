// Load the Desk bridge before configuring its signer.
const deskQzReady = new Promise(resolve => {
    const loading = frappe.require('/assets/ury/js/qz-tray.js', resolve);
    if (loading && typeof loading.then === 'function') loading.then(resolve, resolve);
});

async function printDeskQzInvoice(invoice, profile, safePrintFormat, configuredLetterHead, onPrinted = updateMergedInvoicePrint) {
    let signing;
    let printed = false;
    try {
        signing = await import('/assets/ury/js/qz-signing.js');
        await deskQzReady;
        const { message } = await frappe.call({
            method: "frappe.www.printview.get_html_and_style",
            args: {
                doc: "POS Invoice",
                name: invoice,
                print_format: safePrintFormat,
                no_letterhead: configuredLetterHead ? 0 : 1,
                letterhead: configuredLetterHead || "No Letterhead",
                _lang: 'en',
            },
        });
        if (!message?.html) throw new Error("Invoice print content is unavailable");

        await signing.runSignedQz(qz, (method, args) => frappe.call({ method, args, silent: true }), async () => {
            await signing.connectQz(qz, async () => {
                const { message } = await frappe.call({ method: "ury.ury.api.ury_print.qz_certificate" });
                return message;
            }, profile.qz_host);
            const printer = profile.custom_qz_receipt_queue?.trim() || await qz.printers.getDefault();
            await qz.print(qz.configs.create(printer), [{ type: 'html', format: 'plain', data: message.html }]);
        });
        printed = true;
        await onPrinted(invoice);
    } catch (error) {
        let message = __("Could not print the invoice. Check the printer and try again.");
        if (printed) {
            message = __("The invoice printed, but its status could not be updated. Check the receipt before retrying.");
        } else if (signing && error instanceof signing.QzSigningRefusedError) {
            message = __("Printer signing was refused. Check your printing permissions and try again.");
        }
        frappe.msgprint({ title: __("Printing Failed"), message, indicator: 'red' });
    } finally {
        frappe.dom.unfreeze();
    }
}

async function updateMergedInvoicePrint(invoice) {

    await frappe.call({
        method: "ury.ury.api.ury_print.qz_print_update",
        args: {
            invoice: invoice,
        },
    });

    await frappe.call({
        method:
            "ury.ury.doctype.ury_order.ury_order.release_tables_after_print",
        args: {
            invoice: invoice,
        },
    });

    cur_frm.set_value(
        "invoice_printed",
        1,
    );

    cur_frm.reload_doc();

    frappe.show_alert({
        message: __("Invoice Printed"),
        indicator: "green",
    });
}

const UNSAFE_POS_PRINT_FORMATS = new Set([
    'POS Invoice Classic',
    'POS Invoice Bordered',
    'POS Invoice Modern',
    'POS Invoice Modern with Images',
    'POS Invoice',
    'Return POS Invoice',
    'EFRIS POS Receipt 80mm',
    'POS EFRIS Invoice',
]);

function resolveSafePosPrintFormat(configured) {
    const clean = (configured || '').trim();
    if (!clean || UNSAFE_POS_PRINT_FORMATS.has(clean)) {
        return 'Merged POS Invoice Format';
    }
    return clean;
}

frappe.ui.form.on('POS Invoice', {

    before_save: function (frm) {
        if (frm.doc.customer_name === null || frm.doc.customer_name === "") {
            frappe.throw({
                message: __("Failed to load data . Please refresh the page")
            });
        }
    },

    print: function (frm) {
        const invoice = frm.doc.name;
        return frappe.db.get_doc('POS Invoice', invoice).then(pos_invoice => {
            if (pos_invoice.invoice_printed == 1) {
                frappe.throw({
                    title: __("Invoice Already Billed"),
                    message: __("This order has already been billed. Please reload the page."),
                    indicator: 'red'
                });
            }
            return frappe.db.get_doc('POS Profile', frm.doc.pos_profile).then(profile => {
                const safePrintFormat = resolveSafePosPrintFormat(profile.print_format);
                const configuredLetterHead = (profile.letter_head || '').trim();

                frappe.dom.freeze(__('Printing Invoice'));
                if (profile.qz_print == 1) {
                    return printDeskQzInvoice(invoice, profile, safePrintFormat, configuredLetterHead);
                }
                else if (profile.printer_settings.some(e => e.bill == 1)) {
                    profile.printer_settings.forEach(print => {
                        frappe.call({
                            method: `ury.ury.api.ury_print.network_printing`,
                            args: {
                                doctype: "POS Invoice",
                                name: invoice,
                                printer_setting: print.printer,
                                print_format: safePrintFormat
                            },
                            callback: function (r) {
                                if (r.message == "Success") {
                                    $('.standard-actions').addClass('hidden-xs hidden-md');
                                    frappe.show_alert({ message: __('Invoice Printed'), indicator: 'green' });
                                    updateMergedInvoicePrint(invoice);
                                    frappe.dom.unfreeze();
                                    cur_frm.reload_doc();
                                }
                                else {
                                    console.error(r.message);
                                    frappe.dom.unfreeze();
                                    frappe.throw({
                                        message: __("Printing Failed")
                                    });
                                }
                            },
                            error: function (xhr, textStatus, error) {
                                console.error("AJAX Error:", error); // Log the AJAX error
                                frappe.dom.unfreeze();
                                frappe.throw({
                                    message: __("An error occurred while printing")
                                });
                            }
                        })
                    })
                }
                else {
                    frappe.call({
                        method: `ury.ury.api.ury_print.print_pos_page`,
                        args: {
                            doctype: "POS Invoice",
                            name: invoice,
                            print_format: safePrintFormat
                        },
                        callback: function (r) {
                            $('.standard-actions').addClass('hidden-xs hidden-md');
                            updateMergedInvoicePrint(invoice);
                            frappe.show_alert({ message: __('Invoice Printed'), indicator: 'green' });
                            frappe.ui.toolbar.clear_cache()
                            frappe.dom.unfreeze();
                        }
                    });
                }
            })
        })
    }

})

