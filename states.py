from aiogram.fsm.state import State, StatesGroup


class AddProductState(StatesGroup):
    waiting_for_name = State()
    waiting_for_link = State()


class RemoveProductState(StatesGroup):
    waiting_for_product_id = State()
