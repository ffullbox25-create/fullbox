# Спецификация `warehouse_state.py`

## Назначение

Этот документ описывает первый прикладной слой новой складской архитектуры: модуль `warehouse_state.py`.

Его задача:

- дать системе единый способ отвечать на вопрос “что сейчас происходит с товаром”
- перестать собирать статусы и состояние товара в разных модулях по-разному
- создать переходный слой над текущими таблицами без немедленной перестройки всей БД

Это **не** финальная архитектура склада. Это первый безопасный технический шаг к ней.

## Цель модуля

Модуль `warehouse_state.py` должен стать единым слоем чтения для:

- статусов товара
- доступности товара
- активных складских операций
- готовности товара к следующему этапу

Сразу важно зафиксировать:

- модуль **не пишет** бизнес-документы
- модуль **не меняет** текущие процессы сам по себе
- модуль **считывает** факты из текущих таблиц и приводит их к единой нормализованной модели

## Что должен решать модуль

Сейчас разные экраны отвечают на разные вопросы по-разному:

- где товар
- зарезервирован ли он
- едет ли он в OBR
- доставлен ли он в OTG
- готов ли он к погрузке
- загружен ли он в машину

`warehouse_state.py` должен отвечать на это одинаково для всех модулей.

## Границы ответственности

`warehouse_state.py` отвечает только за складскую реальность.

Он должен отвечать на вопросы:

- где товар физически
- в каком он контейнере
- какой резерв на него повешен
- какая складская операция идет
- завершено ли это движение
- какой normalized warehouse state у товара или заказа

Он **не** отвечает на вопросы:

- менеджер согласовал документ или нет
- клиент подписал акт или нет
- логист утвердил рейс или нет

Эти вопросы остаются в бизнес-модулях.

## Место в проекте

Рекомендуемый путь:

- `fullbox/sklad/services/warehouse_state.py`

С ним рядом позже могут появиться:

- `fullbox/sklad/services/warehouse_availability.py`
- `fullbox/sklad/services/warehouse_movement.py`
- `fullbox/sklad/services/warehouse_events.py`

Но первый шаг лучше сделать в одном модуле, чтобы не распыляться.

## Нормализованные складские состояния

Это не статусы БД и не тексты для UI. Это внутренняя нормализованная модель.

### Базовый enum

Рекомендуемое имя:

- `WarehouseStateCode`

Рекомендуемые значения:

- `UNKNOWN`
- `RECEIVED_UNPLACED`
- `PLACED_IN_RECEIVING`
- `STORED`
- `RESERVED_FOR_PROCESSING`
- `MOVING_TO_PROCESSING`
- `IN_PROCESSING_ZONE`
- `PROCESSING_IN_PROGRESS`
- `PLACED_AFTER_PROCESSING`
- `RESERVED_FOR_SHIPPING`
- `MOVING_TO_OTG`
- `IN_OTG`
- `PALLETIZING`
- `READY_FOR_LOADING`
- `ASSIGNED_TO_TRIP`
- `LOADING_IN_PROGRESS`
- `LOADED_TO_VEHICLE`
- `SHIPPED`
- `PARTIALLY_SHIPPED`
- `CANCELED`

### Смысл состояний

#### `RECEIVED_UNPLACED`

Товар поступил, но еще не оформлен размещением.

#### `PLACED_IN_RECEIVING`

Товар размещен актом, физически существует как складской остаток, но еще не обязательно поставлен в ячейки хранения.

#### `STORED`

Товар физически находится на складе, доступен в хранении.

#### `RESERVED_FOR_PROCESSING`

Товар зарезервирован под обработку, но еще не доставлен в OBR.

#### `MOVING_TO_PROCESSING`

Для товара есть активная складская операция на перемещение в OBR.

#### `IN_PROCESSING_ZONE`

Товар уже доставлен в OBR.

#### `PROCESSING_IN_PROGRESS`

Обработка началась и товар фактически участвует в обработке.

#### `PLACED_AFTER_PROCESSING`

Результат обработки заново размещен и снова стал складским остатком.

#### `RESERVED_FOR_SHIPPING`

Товар зарезервирован под отгрузку, но еще не едет в OTG.

#### `MOVING_TO_OTG`

Есть активные задания ричтраку на доставку товара в OTG.

#### `IN_OTG`

Товар доставлен в OTG.

#### `PALLETIZING`

Товар находится на стадии формирования отгрузочных паллет.

#### `READY_FOR_LOADING`

Товар подготовлен складом и готов к погрузке.

#### `ASSIGNED_TO_TRIP`

Товар привязан к рейсу, но еще не загружается в машину.

#### `LOADING_IN_PROGRESS`

Рейс находится на стадии погрузки и товар входит в этот процесс.

#### `LOADED_TO_VEHICLE`

Товар физически загружен в машину.

#### `SHIPPED`

Товар выбыл со склада.

#### `PARTIALLY_SHIPPED`

Отгружена только часть.

#### `CANCELED`

Документ отменен и складская потребность снята.

## Уровни вычисления состояния

Важно вычислять состояние не “в целом по системе”, а по разным объектам.

Нужно три уровня:

1. состояние единицы товара / SKU-среза
2. состояние складского контекста
3. состояние документа в складском смысле

### 1. Goods-level state

Вопрос:

- что происходит с конкретным товаром или срезом SKU/size/barcode/goods_type

### 2. Context-level state

Вопрос:

- что происходит с набором товара в рамках одной приемки, обработки, отгрузки, рейса

### 3. UI-facing warehouse state

Вопрос:

- какой текст нужно показать конкретной роли

## Основные resolver'ы

### 1. `WarehouseGoodsStateResolver`

Главный resolver.

Он должен уметь отвечать:

- в каком нормализованном warehouse-state находится товар
- из каких фактов это выведено
- какая операция активна
- какой резерв влияет

#### Предлагаемый интерфейс

```python
class WarehouseGoodsStateResolver:
    @classmethod
    def resolve_for_shipping_order(cls, order) -> WarehouseStateResult: ...

    @classmethod
    def resolve_for_processing_order(cls, order_id: str, agency) -> WarehouseStateResult: ...

    @classmethod
    def resolve_for_receiving_order(cls, order_id: str, agency) -> WarehouseStateResult: ...

    @classmethod
    def resolve_for_trip(cls, trip) -> WarehouseStateResult: ...

    @classmethod
    def resolve_for_stock_identity(
        cls,
        *,
        agency,
        sku_code: str,
        size: str = "",
        barcode: str = "",
        goods_type: str = "",
    ) -> WarehouseStateResult: ...
```

### 2. `WarehouseAvailabilityResolver`

Он должен отвечать:

- сколько товара есть физически
- сколько зарезервировано под обработку
- сколько зарезервировано под отгрузку
- сколько реально доступно

#### Предлагаемый интерфейс

```python
class WarehouseAvailabilityResolver:
    @classmethod
    def resolve(
        cls,
        *,
        agency,
        sku_code: str,
        size: str = "",
        barcode: str = "",
        goods_type: str = "",
    ) -> WarehouseAvailabilityResult: ...
```

### 3. `WarehouseMovementResolver`

Он должен отвечать:

- есть ли активные складские операции
- какие задачи открыты
- куда везут товар
- завершено ли движение

#### Предлагаемый интерфейс

```python
class WarehouseMovementResolver:
    @classmethod
    def active_for_shipping_order(cls, order) -> WarehouseMovementResult: ...

    @classmethod
    def active_for_processing_order(cls, order_id: str, agency) -> WarehouseMovementResult: ...

    @classmethod
    def active_for_receiving_order(cls, order_id: str, agency) -> WarehouseMovementResult: ...
```

## Result-объекты

### `WarehouseStateResult`

Рекомендуемая структура:

```python
@dataclass
class WarehouseStateResult:
    code: WarehouseStateCode
    label_default: str
    label_client: str
    label_storekeeper: str
    label_logistician: str
    label_processing: str
    source_facts: list[str]
    location_summary: dict
    reserve_summary: dict
    movement_summary: dict
    is_terminal: bool
    is_ready_for_next_step: bool
```

### `WarehouseAvailabilityResult`

```python
@dataclass
class WarehouseAvailabilityResult:
    physical_qty: int
    processing_reserved_qty: int
    shipping_reserved_qty: int
    active_movement_hold_qty: int
    available_qty: int
    source_rows_count: int
```

### `WarehouseMovementResult`

```python
@dataclass
class WarehouseMovementResult:
    has_active_tasks: bool
    active_task_count: int
    done_task_count: int
    blocked_task_count: int
    destination_zone: str
    operation_kind: str
    task_ids: list[str]
```

## Из каких таблиц считать данные

Это ключевая часть: модуль должен работать поверх текущей системы.

### Основные источники данных

#### `StockPalletState`

Используем для:

- физического остатка
- контейнеров
- location
- materialized reserve fields
- доступности

#### `InventoryState`

Используем для:

- резерва обработки
- остатка потребности, еще не доставленной в OBR

#### `ShippingReserve`

Используем для:

- резерва отгрузки

#### `MoveTask`

Используем для:

- активных перемещений
- завершенных перемещений
- определения направления движения: `OBR`, `OTG`, внутренняя перестановка

#### `MoveRequest`

Используем для:

- группировки задач в одну warehouse-операцию

#### `OrderAuditEntry`

Используем временно только там, где еще нет нормальной складской таблицы:

- processing work payload
- shipping packing payload
- trip loading payload
- placement payload как переходный источник некоторых фактов

#### `LogisticsTrip` и `LogisticsTripOrder`

Используем для:

- определения привязки к рейсу
- стадий `ASSIGNED_TO_TRIP`, `LOADING_IN_PROGRESS`

#### `MarkingCode`

Используем для:

- состояний, где наличие/бронь/использование ЧЗ влияет на обработку

## Таблица соответствия: состояние -> откуда считается

### `RECEIVED_UNPLACED`

Источник:

- есть receiving document
- нет закрытого placement
- в `StockPalletState` еще нет строк для этого контекста

### `PLACED_IN_RECEIVING`

Источник:

- есть закрытый placement
- есть строки `StockPalletState` по `order_type=receiving`
- активного перемещения в OBR или OTG нет

### `STORED`

Источник:

- есть `StockPalletState`
- нет активного processing reserve
- нет активного shipping reserve
- нет активного move task к OBR/OTG

### `RESERVED_FOR_PROCESSING`

Источник:

- есть `InventoryState` для processing order
- нет активного `MoveTask` в `OBR`

### `MOVING_TO_PROCESSING`

Источник:

- есть `MoveTask` со статусом `created` или `in_progress`
- `to_zone == "OBR"`

### `IN_PROCESSING_ZONE`

Источник:

- задачи в `OBR` завершены
- резерв под обработку уже частично или полностью потреблен
- товар находится в зоне обработки или еще не возвращен в складской остаток

### `PROCESSING_IN_PROGRESS`

Источник:

- processing work payload говорит, что обработка начата / не завершена
- есть признаки активной processing session

### `PLACED_AFTER_PROCESSING`

Источник:

- закрыт processing placement
- результат записан в `StockPalletState`

### `RESERVED_FOR_SHIPPING`

Источник:

- есть `ShippingReserve`
- нет активных OTG tasks

### `MOVING_TO_OTG`

Источник:

- есть `MoveTask` со статусом `created` или `in_progress`
- `to_zone == "OTG"`

### `IN_OTG`

Источник:

- OTG tasks завершены
- shipping order еще не упакован окончательно

### `PALLETIZING`

Источник:

- OTG delivery завершена
- packing started / packing payload существует
- packed state еще не завершен

### `READY_FOR_LOADING`

Источник:

- shipping order в packed state
- рейс еще не начал loading

### `ASSIGNED_TO_TRIP`

Источник:

- есть `LogisticsTripOrder`
- статус рейса `draft` или `planned`

### `LOADING_IN_PROGRESS`

Источник:

- рейс в статусе `loading`

### `LOADED_TO_VEHICLE`

Источник:

- trip loading payload показывает, что конкретные паллеты загружены
- или рейс `departed` и order фактически загружен

### `SHIPPED`

Источник:

- shipping order в `STATUS_SHIPPED`
- товар списан со склада

### `PARTIALLY_SHIPPED`

Источник:

- shipping order в `STATUS_PARTIAL`

### `CANCELED`

Источник:

- документ отменен
- активные резервы сняты

## Правила приоритета состояний

Если одновременно срабатывают несколько условий, нужен строгий порядок.

Рекомендуемый приоритет сверху вниз:

1. `CANCELED`
2. `SHIPPED`
3. `PARTIALLY_SHIPPED`
4. `LOADED_TO_VEHICLE`
5. `LOADING_IN_PROGRESS`
6. `ASSIGNED_TO_TRIP`
7. `READY_FOR_LOADING`
8. `PALLETIZING`
9. `IN_OTG`
10. `MOVING_TO_OTG`
11. `RESERVED_FOR_SHIPPING`
12. `PLACED_AFTER_PROCESSING`
13. `PROCESSING_IN_PROGRESS`
14. `IN_PROCESSING_ZONE`
15. `MOVING_TO_PROCESSING`
16. `RESERVED_FOR_PROCESSING`
17. `STORED`
18. `PLACED_IN_RECEIVING`
19. `RECEIVED_UNPLACED`
20. `UNKNOWN`

Это нужно, чтобы результат был детерминированным.

## Правила для UI-подписей

`warehouse_state.py` не должен отдавать только один текст.

Лучше сразу проектировать несколько представлений:

- default
- client
- storekeeper
- logistician
- processing

Пример:

`IN_OTG`

- default: `Товар в зоне отгрузки`
- client: `Товар подготовлен складом`
- storekeeper: `Товар доставлен в OTG, ожидает паллетизации`
- logistician: `Ожидает формирования отгрузочных паллет`

То есть один код состояния, но разные формулировки.

## Что должно остаться временно “через адаптер”

Чтобы не ломать систему сразу, `warehouse_state.py` на первом этапе должен считать через адаптеры:

- processing reserve adapter над `InventoryState`
- shipping reserve adapter над `ShippingReserve`
- trip loading adapter над `OrderAuditEntry`
- packing adapter над `OrderAuditEntry`

Это важно: на первом этапе модуль должен **объединять правду**, а не требовать немедленной миграции таблиц.

## Минимальный объем первой реализации

Первую реализацию лучше ограничить.

### Версия 1

Сделать только:

- `WarehouseStateCode`
- `WarehouseStateResult`
- `WarehouseGoodsStateResolver.resolve_for_shipping_order(...)`
- `WarehouseMovementResolver.active_for_shipping_order(...)`
- helper-функции по OTG / trip / packed / departed

Почему именно так:

- самая болезненная часть сейчас — отгрузка, OTG, паллетизация, рейс, погрузка
- там уже больше всего рассинхронов между кабинетами
- это даст быстрый эффект без захвата всей системы сразу

### Версия 2

Добавить:

- `resolve_for_processing_order(...)`
- `resolve_for_receiving_order(...)`

### Версия 3

Добавить:

- `WarehouseAvailabilityResolver`
- общие adapters по резервам

## Где потом подключать модуль

### Первая волна интеграции

- `shipping/selectors.py`
- `todo/templatetags/todo_panel.py`
- `logistics/views.py`

### Вторая волна интеграции

- client cabinet
- processing screens
- receiving screens

## Что не надо делать на первом шаге

Не надо сразу:

- вводить новые большие миграции БД
- удалять `InventoryState`
- удалять `ShippingReserve`
- переписывать `OrderAuditEntry`
- менять все процессы записи

Первый шаг — это **единое чтение и единая интерпретация**, а не переписывание всей системы.

## Предлагаемая структура модуля

```python
from dataclasses import dataclass
from enum import StrEnum


class WarehouseStateCode(StrEnum):
    ...


@dataclass
class WarehouseStateResult:
    ...


@dataclass
class WarehouseAvailabilityResult:
    ...


@dataclass
class WarehouseMovementResult:
    ...


class WarehouseGoodsStateResolver:
    ...


class WarehouseAvailabilityResolver:
    ...


class WarehouseMovementResolver:
    ...
```

Дополнительно внутри модуля:

- private helper'ы для чтения reserves
- private helper'ы для MoveTask progress
- private helper'ы для logistics trip status
- private helper'ы для packing state

## Главный результат этой спецификации

После появления `warehouse_state.py`:

- все кабинеты перестанут самостоятельно “толковать” движение товара
- складское состояние начнет вычисляться единообразно
- текущие таблицы останутся рабочими
- мы получим безопасную точку входа для следующего этапа рефакторинга

Именно поэтому это лучший следующий шаг после архитектурных документов: он уже очень прикладной, но еще не ломает прод-процессы.
