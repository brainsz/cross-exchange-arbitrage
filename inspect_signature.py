import inspect
from bpx.account import Account

print(inspect.signature(Account.get_order_history_query))
print(inspect.signature(Account.get_fill_history_query))
