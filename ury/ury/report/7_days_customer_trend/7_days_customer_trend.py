from ury.ury.api.kitchen_ticket_history import run_ticket_report


def execute(filters=None):
    return run_ticket_report("7 Days Customer Trend", filters)
