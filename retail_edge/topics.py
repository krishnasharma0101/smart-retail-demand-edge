"""MQTT topic map (single source of truth for edge service + simulator)."""
from .config import settings

S = settings.store_id
ROOT = f"retail/{S}"
POS = ROOT + "/pos/{sku}"                  # store -> edge : point-of-sale line {qty, price}
SHELF = ROOT + "/shelf/{sku}"              # store -> edge : shelf sensor {on_hand}
CLOCK = ROOT + "/clock"                    # store -> edge : day boundary {sim_date, event}
DELIVERY = ROOT + "/delivery/{sku}"        # store -> edge : goods received {qty}
FORECAST = ROOT + "/edge/forecast/{sku}"   # edge  -> any  : retained forecast
ALERT = ROOT + "/edge/alert/{sku}"         # edge  -> any  : retained replenishment alert
REORDER = ROOT + "/edge/cmd/reorder/{sku}" # edge  -> store: purchase order {qty, po}
STATUS = ROOT + "/edge/status"             # edge  -> any  : retained online/offline (LWT)
SIM_STATUS = ROOT + "/sim/status"

SUBSCRIPTIONS = [ROOT + "/pos/+", ROOT + "/shelf/+", CLOCK, ROOT + "/delivery/+", SIM_STATUS]
