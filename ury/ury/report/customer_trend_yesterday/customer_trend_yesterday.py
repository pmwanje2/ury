from ury.ury.api.kitchen_ticket_history import run_ticket_report


def execute(filters=None):
    return run_ticket_report("Customer Trend Yesterday", filters)
