from aiogram.fsm.state import State, StatesGroup

class AddProduct(StatesGroup):
    waiting_for_name = State()
    waiting_for_link = State()
