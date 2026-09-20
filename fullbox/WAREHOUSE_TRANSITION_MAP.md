# Карта переходов складских состояний

## Назначение

Этот документ фиксирует переходы между нормализованными warehouse-state.

Он нужен для того, чтобы:

- перестать переводить товар между состояниями неявно;
- закрепить, какие события действительно меняют текущее состояние;
- определить допустимые и недопустимые переходы;
- подготовить основу для будущего write-path, в котором `WarehouseEvent` и `WarehouseOperation` будут обновлять `WarehouseStockSnapshot` предсказуемо.

Этот документ является следующим шагом после:

- [WAREHOUSE_EVENTS_DICTIONARY.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_EVENTS_DICTIONARY.md)
- [WAREHOUSE_STATE_SPEC.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_STATE_SPEC.md)

## Главная идея

Текущее состояние товара не должно определяться:

- UI-логикой;
- случайным сочетанием флагов документа;
- последней удобной строкой из audit payload.

Текущее состояние должно меняться только по понятным правилам:

- есть исходный `warehouse_state`;
- происходит каноническое событие;
- система переводит товар в новый `warehouse_state`;
- обновляет materialized snapshot.

## Канонические warehouse-state

Ниже используется набор состояний из [WAREHOUSE_STATE_SPEC.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_STATE_SPEC.md).

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

## Базовые правила переходов

### 1. Состояние меняется только от warehouse event

Если состояние товара изменилось, это должно быть связано с событием из [WAREHOUSE_EVENTS_DICTIONARY.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_EVENTS_DICTIONARY.md).

### 2. Переход должен отражать физическую или операционную реальность

Пример правильного перехода:

- `STORED` + `processing_reserved` -> `RESERVED_FOR_PROCESSING`

Пример неправильного перехода:

- `STORED` + “логист нажал кнопку” -> `LOADED_TO_VEHICLE`

### 3. Документ может инициировать переход, но не заменяет его

Например:

- заявка на отгрузку может создать `shipping_reserved`;
- но товар не станет `IN_OTG`, пока не произойдет `otg_arrived`.

### 4. Нельзя перепрыгивать через физические этапы без явного события

Нельзя делать так:

- `STORED` -> `READY_FOR_LOADING`
- `RESERVED_FOR_PROCESSING` -> `PROCESSING_IN_PROGRESS`
- `ASSIGNED_TO_TRIP` -> `SHIPPED`

если не было промежуточных канонических событий.

## Переходы по жизненному циклу

## 1. Поступление и размещение

### `UNKNOWN`

Допустимые события:

- `receiving_arrived` -> `RECEIVED_UNPLACED`
- `warehouse_context_canceled` -> `CANCELED`

### `RECEIVED_UNPLACED`

Допустимые события:

- `placement_started` -> `RECEIVED_UNPLACED`
- `placement_completed` -> `PLACED_IN_RECEIVING`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- `placement_started` само по себе еще не создает новый физический факт, поэтому состояние можно не менять.

### `PLACED_IN_RECEIVING`

Допустимые события:

- `putaway_requested` -> `PLACED_IN_RECEIVING`
- `movement_started` в контексте `putaway` -> `PLACED_IN_RECEIVING`
- `putaway_completed` -> `STORED`
- `movement_completed` с целевой зоной хранения -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- пока товар только размещен в приемочной зоне, он еще не считается полноценно размещенным в хранении.

## 2. Хранение как базовая точка

### `STORED`

Допустимые события:

- `processing_reserved` -> `RESERVED_FOR_PROCESSING`
- `shipping_reserved` -> `RESERVED_FOR_SHIPPING`
- `movement_requested` типа `internal_relocation` -> `STORED`
- `movement_started` типа `internal_relocation` -> `STORED`
- `movement_completed` типа `internal_relocation` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- внутреннее перемещение внутри хранения может не менять high-level состояние, хотя меняет физическую локацию.

## 3. Обработка

### `RESERVED_FOR_PROCESSING`

Допустимые события:

- `processing_reserve_released` -> `STORED`
- `movement_requested` типа `move_to_processing` -> `RESERVED_FOR_PROCESSING`
- `movement_started` типа `move_to_processing` -> `MOVING_TO_PROCESSING`
- `movement_canceled` типа `move_to_processing` -> `RESERVED_FOR_PROCESSING`
- `warehouse_context_canceled` -> `CANCELED`

### `MOVING_TO_PROCESSING`

Допустимые события:

- `movement_completed` с целевой зоной `OBR` -> `IN_PROCESSING_ZONE`
- `processing_zone_arrived` -> `IN_PROCESSING_ZONE`
- `movement_canceled` -> `RESERVED_FOR_PROCESSING`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

### `IN_PROCESSING_ZONE`

Допустимые события:

- `processing_started` -> `PROCESSING_IN_PROGRESS`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- сам факт доставки в `OBR` еще не означает, что обработка началась.

### `PROCESSING_IN_PROGRESS`

Допустимые события:

- `processing_completed` -> `PLACED_AFTER_PROCESSING`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- результат обработки должен быть выражен новым складским фактом, а не просто флагом в заказе.

### `PLACED_AFTER_PROCESSING`

Допустимые события:

- `putaway_requested` -> `PLACED_AFTER_PROCESSING`
- `movement_started` типа `putaway` -> `PLACED_AFTER_PROCESSING`
- `movement_completed` с целевой зоной хранения -> `STORED`
- `shipping_reserved` -> `RESERVED_FOR_SHIPPING`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- если результат обработки уже готов к отгрузке без возврата в обычное хранение, допускается переход сразу в резерв под отгрузку.

## 4. Отгрузка и `OTG`

### `RESERVED_FOR_SHIPPING`

Допустимые события:

- `shipping_reserve_released` -> `STORED`
- `otg_requested` -> `RESERVED_FOR_SHIPPING`
- `movement_requested` типа `move_to_otg` -> `RESERVED_FOR_SHIPPING`
- `movement_started` типа `move_to_otg` -> `MOVING_TO_OTG`
- `movement_canceled` типа `move_to_otg` -> `RESERVED_FOR_SHIPPING`
- `warehouse_context_canceled` -> `CANCELED`

### `MOVING_TO_OTG`

Допустимые события:

- `movement_completed` с целевой зоной `OTG` -> `IN_OTG`
- `otg_arrived` -> `IN_OTG`
- `movement_canceled` -> `RESERVED_FOR_SHIPPING`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

### `IN_OTG`

Допустимые события:

- `palletization_started` -> `PALLETIZING`
- `ready_for_loading` -> `READY_FOR_LOADING`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- для простых сценариев допускается переход из `IN_OTG` сразу в `READY_FOR_LOADING`, если отдельной паллетизации фактически нет.

### `PALLETIZING`

Допустимые события:

- `palletization_completed` -> `READY_FOR_LOADING`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

### `READY_FOR_LOADING`

Допустимые события:

- `assigned_to_trip` -> `ASSIGNED_TO_TRIP`
- `loading_started` -> `LOADING_IN_PROGRESS`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- назначение на рейс и начало погрузки — не одно и то же.

## 5. Логистика и выбытие

### `ASSIGNED_TO_TRIP`

Допустимые события:

- `loading_started` -> `LOADING_IN_PROGRESS`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

### `LOADING_IN_PROGRESS`

Допустимые события:

- `loaded_to_vehicle` -> `LOADED_TO_VEHICLE`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- если загрузка сорвалась, возврат товара должен быть оформлен как отдельный складской факт.

### `LOADED_TO_VEHICLE`

Допустимые события:

- `shipped` -> `SHIPPED`
- `stock_returned_to_storage` -> `STORED`
- `warehouse_context_canceled` -> `CANCELED`

Комментарий:
- товар может быть загружен, но еще не считаться окончательно выбывшим до завершения рейсового события.

### `SHIPPED`

Допустимые события:

- нет обычных прямых переходов;
- допустимы только специальные корректировочные сценарии через отдельные возвратные контуры.

Комментарий:
- это терминальное состояние для стандартного потока.

### `PARTIALLY_SHIPPED`

Допустимые события:

- `loaded_to_vehicle` -> `PARTIALLY_SHIPPED`
- `shipped` -> `SHIPPED`
- `stock_returned_to_storage` -> `STORED`

Комментарий:
- частичная отгрузка должна жить только там, где это действительно поддерживается физическим учетом количества.

### `CANCELED`

Допустимые события:

- нет обычных прямых переходов;
- возможны только специальные ручные восстановительные сценарии.

Комментарий:
- отмена должна закрывать активные операции и резервы, а не оставлять “подвисшее” состояние.

## Недопустимые переходы

Ниже перечислены переходы, которые следует считать ошибочными на уровне warehouse kernel.

### Недопустимые переходы обработки

- `STORED` -> `IN_PROCESSING_ZONE` без `movement_completed` / `processing_zone_arrived`
- `RESERVED_FOR_PROCESSING` -> `PROCESSING_IN_PROGRESS` без доставки в `OBR`
- `IN_PROCESSING_ZONE` -> `PLACED_AFTER_PROCESSING` без `processing_completed`

### Недопустимые переходы отгрузки

- `STORED` -> `IN_OTG` без события перемещения
- `RESERVED_FOR_SHIPPING` -> `READY_FOR_LOADING` без `otg_arrived` или эквивалентного складского факта
- `IN_OTG` -> `ASSIGNED_TO_TRIP` без складского факта готовности к погрузке

### Недопустимые переходы логистики

- `READY_FOR_LOADING` -> `LOADED_TO_VEHICLE` без `loading_started` или без прямого подтвержденного складского события загрузки
- `ASSIGNED_TO_TRIP` -> `SHIPPED` без `loaded_to_vehicle`
- `STORED` -> `SHIPPED` напрямую

### Недопустимые общие переходы

- любой переход в `SHIPPED` без факта физического выбытия;
- любой переход в `CANCELED`, если активные резервы и операции не закрыты;
- любое изменение состояния только по UI-флагу без warehouse event.

## Таблица переходов по событиям

| Текущее состояние | Событие | Новое состояние | Примечание |
| --- | --- | --- | --- |
| `UNKNOWN` | `receiving_arrived` | `RECEIVED_UNPLACED` | старт жизненного цикла |
| `RECEIVED_UNPLACED` | `placement_completed` | `PLACED_IN_RECEIVING` | появляется первичный складской факт |
| `PLACED_IN_RECEIVING` | `putaway_completed` | `STORED` | товар попадает в хранение |
| `STORED` | `processing_reserved` | `RESERVED_FOR_PROCESSING` | резерв под обработку |
| `RESERVED_FOR_PROCESSING` | `movement_started` в `OBR` | `MOVING_TO_PROCESSING` | начата доставка в обработку |
| `MOVING_TO_PROCESSING` | `processing_zone_arrived` | `IN_PROCESSING_ZONE` | товар доставлен в `OBR` |
| `IN_PROCESSING_ZONE` | `processing_started` | `PROCESSING_IN_PROGRESS` | начата обработка |
| `PROCESSING_IN_PROGRESS` | `processing_completed` | `PLACED_AFTER_PROCESSING` | завершение обработки |
| `PLACED_AFTER_PROCESSING` | `movement_completed` в хранение | `STORED` | результат обработки возвращен |
| `STORED` | `shipping_reserved` | `RESERVED_FOR_SHIPPING` | резерв под отгрузку |
| `RESERVED_FOR_SHIPPING` | `movement_started` в `OTG` | `MOVING_TO_OTG` | начата доставка в `OTG` |
| `MOVING_TO_OTG` | `otg_arrived` | `IN_OTG` | товар в `OTG` |
| `IN_OTG` | `palletization_started` | `PALLETIZING` | начата паллетизация |
| `PALLETIZING` | `palletization_completed` | `READY_FOR_LOADING` | товар готов к погрузке |
| `READY_FOR_LOADING` | `assigned_to_trip` | `ASSIGNED_TO_TRIP` | товар закреплен за рейсом |
| `ASSIGNED_TO_TRIP` | `loading_started` | `LOADING_IN_PROGRESS` | начата загрузка |
| `LOADING_IN_PROGRESS` | `loaded_to_vehicle` | `LOADED_TO_VEHICLE` | товар в машине |
| `LOADED_TO_VEHICLE` | `shipped` | `SHIPPED` | товар выбыл |

## Что это означает для реализации

Из этой карты следуют три практических правила.

### 1. `warehouse_state.py` не должен сам придумывать переходы

Он должен читать уже материализованный `warehouse_state_code`, а не заново сочинять состояние по косвенным признакам.

### 2. Write-path должен валидировать переходы

Если приходит событие, которое пытается перевести товар в недопустимое состояние, система должна:

- отклонять такой переход;
- либо направлять его в отдельный корректировочный сценарий.

### 3. Snapshot должен быть следствием событий, а не альтернативной жизнью системы

`WarehouseStockSnapshot` должен обновляться не вручную “где удобно”, а через понятный набор transition rules.

## Следующий шаг

Следующий инженерный документ должен описать первый write-path.

Я бы начал с самого базового и честного контура:

1. `приемка -> placement_completed -> putaway_requested -> putaway_completed -> STORED`
2. затем `обработка -> processing_reserved -> movement_to_OBR -> processing_started -> processing_completed`
3. и только потом `отгрузка -> OTG -> паллетизация -> погрузка`

То есть следующим шагом уже логично проектировать не общий UI-resolver, а первый реальный складской write-path от события до обновления snapshot.
