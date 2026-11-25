frappe.ui.form.on('POS Closing Entry', {
    // Client-side logic for stock correction validation has been moved server-side.
    refresh(frm) {
        if (frm.doc.docstatus !== 1) return;
        const canPushTips =
            frappe.user.has_role('HR Manager') || frappe.user.has_role('Accounts Manager');
        if (!canPushTips) return;
        const pendingRows = (frm.doc.custom_tip_declarations || []).filter(
            (row) => flt(row.net_tips_payable) > 0 && row.employee && !row.custom_additional_salary_ref
        );
        if (!pendingRows.length) return;
        frm.add_custom_button(__('Create Tip Additional Salaries'), () => {
            frappe.prompt(
                [
                    {
                        fieldname: 'salary_component',
                        fieldtype: 'Data',
                        label: __('Tips Payroll Component (optional override)'),
                        description: __('Leave blank to use the POS Profile configured Tips Payroll Component.'),
                    },
                ],
                (values) => {
                    frappe.call({
                        method: 'ury.ury_pos.tips_declaration.create_tip_additional_salaries',
                        args: {
                            pos_closing_entry: frm.doc.name,
                            salary_component: values.salary_component || undefined,
                        },
                        freeze: true,
                        callback(r) {
                            const created = (r.message && r.message.created) || [];
                            frappe.show_alert({
                                message: __('Created {0} Draft Additional Salary record(s).', [created.length]),
                                indicator: 'green',
                            });
                            frm.reload_doc();
                        },
                    });
                },
                __('Create Tip Additional Salaries'),
                __('Create')
            );
        });
    },
});
