from aiogram.fsm.state import State, StatesGroup


class AddProductStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_link = State()


class PinCodeStates(StatesGroup):
    waiting_for_pin = State()


class SearchStates(StatesGroup):
    waiting_for_keyword = State()


class SelectStates(StatesGroup):
    selecting = State()
