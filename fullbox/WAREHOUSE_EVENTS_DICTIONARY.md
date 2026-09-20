# Словарь складских событий

## Назначение

Этот документ фиксирует единый словарь warehouse events для Fullbox.

Он нужен для того, чтобы:

- перестать восстанавливать текущее состояние товара из случайной комбинации таблиц;
- явно определить, какие события происходят с товаром на складе;
- понять, какие события должны обновлять текущий складской снимок;
- подготовить основу для `warehouse_events.py`, `warehouse_operations.py` и materialized warehouse read-model.

Этот документ является следующим шагом после:

- [WAREHOUSE_DOMAIN_MAP.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_DOMAIN_MAP.md)
- [WAREHOUSE_FACT_MATRIX.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_FACT_MATRIX.md)
- [WAREHOUSE_TARGET_MODEL.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_TARGET_MODEL.md)

## Главная идея

Чтобы потом просто смотреть в базу и получать текущее состояние товара, нам нужны два слоя:

1. журнал складских событий;
2. текущий материализованный складской снимок.

События нужны для ответа на вопрос:

- что произошло;
- кто это сделал;
- когда это произошло;
- какой объект это изменило.

Снимок нужен для ответа на вопрос:

- где товар сейчас;
- сколько его сейчас;
- сколько доступно;
- сколько зарезервировано;
- какая операция активна;
- к какому следующему этапу товар готов.

То есть:

- `WarehouseEvent` хранит историю;
- `WarehouseStockSnapshot` хранит текущее состояние;
- обновление snapshot должно происходить как реакция на warehouse events и warehouse operations.

## Базовые правила для событий

### 1. Событие — это факт, а не мнение UI

Правильные события:

- товар поступил;
- товар размещен;
- резерв создан;
- перемещение начато;
- товар прибыл в `OBR`;
- товар прибыл в `OTG`;
- товар загружен в машину.

Неправильные события:

- карточка стала зеленой;
- клиент видит “готово”;
- задача считается срочной.

### 2. Событие должно быть привязано к складскому объекту

Событие должно ссылаться хотя бы на один из контекстов:

- складской снимок;
- резерв;
- складскую операцию;
- складскую задачу;
- бизнес-документ как источник инициирования.

### 3. Событие должно быть нормализованным

Нам не нужен десяток разных вариантов одного и того же факта.

Например:

- не `otg_done`, `delivered_to_otg`, `stock_to_otg_completed`;
- а один канонический тип `otg_arrived`.

### 4. Событие должно менять складскую модель предсказуемо

Если событие произошло, должно быть понятно:

- меняет ли оно `WarehouseStockSnapshot`;
- меняет ли оно `WarehouseReserve`;
- меняет ли оно `WarehouseOperation`;
- вызывает ли оно новый normalized warehouse-state.

## Предлагаемая каноническая сущность

### `WarehouseEvent`

Рекомендуемые поля:

- `event_type`
- `agency_id`
- `stock_context_type`
- `stock_context_id`
- `operation_id`
- `operation_task_id`
- `reserve_id`
- `source_document_type`
- `source_document_id`
- `zone_from`
- `zone_to`
- `payload`
- `performed_by`
- `performed_by_role`
- `occurred_at`
- `created_at`

### Возможные значения `stock_context_type`

- `receiving`
- `processing`
- `shipping`
- `trip`
- `warehouse`

### Возможные значения `source_document_type`

- `receiving_order`
- `processing_order`
- `shipping_order`
- `logistics_trip`
- `transport_note`
- `manual`

## Группы событий

Ниже события разделены не по модулям интерфейса, а по складскому смыслу.

## 1. События поступления и размещения

### `receiving_arrived`

Смысл:
- товар физически прибыл на склад;
- склад признал факт поступления.

Кто создает:
- приемка

Что меняет:
- создает начальный warehouse-контекст;
- еще не обязано создавать окончательный `WarehouseStockSnapshot`

### `placement_started`

Смысл:
- начато размещение товара после приемки.

Кто создает:
- кладовщик в контуре приемки

Что меняет:
- может открыть складскую операцию размещения;
- еще не является окончательной физической правдой

### `placement_completed`

Смысл:
- размещение завершено;
- товар получил первичный контейнерный и зональный факт.

Кто создает:
- приемка / кладовщик

Что меняет:
- создает или обновляет `WarehouseStockSnapshot`;
- обычно переводит товар в состояние `PLACED_IN_RECEIVING` или `STORED`

### `putaway_requested`

Смысл:
- после размещения требуется отвезти товар из приемочной зоны в хранение.

Кто создает:
- приемка

Что меняет:
- создает `WarehouseOperation` типа `putaway`

### `putaway_completed`

Смысл:
- товар прибыл в зону хранения.

Кто создает:
- ричтрак

Что меняет:
- обновляет `WarehouseStockSnapshot.zone/location`;
- переводит товар в `STORED`

## 2. События резерва

### `processing_reserved`

Смысл:
- товар зарезервирован под обработку.

Кто создает:
- обработка

Что меняет:
- создает или обновляет `WarehouseReserve`;
- уменьшает `available_qty` в snapshot

### `processing_reserve_released`

Смысл:
- резерв под обработку снят полностью или частично.

Кто создает:
- обработка

Что меняет:
- обновляет `WarehouseReserve`;
- возвращает количество в `available_qty`

### `shipping_reserved`

Смысл:
- товар зарезервирован под отгрузку.

Кто создает:
- отгрузка

Что меняет:
- создает или обновляет `WarehouseReserve`;
- уменьшает `available_qty`

### `shipping_reserve_released`

Смысл:
- резерв под отгрузку снят полностью или частично.

Кто создает:
- отгрузка

Что меняет:
- обновляет `WarehouseReserve`;
- возвращает количество в `available_qty`

## 3. События планирования и исполнения перемещений

### `movement_requested`

Смысл:
- возникла потребность в складском перемещении.

Кто создает:
- приемка
- обработка
- отгрузка
- склад вручную

Что меняет:
- создает `WarehouseOperation`

### `movement_task_created`

Смысл:
- создана конкретная исполнимая задача по паллете, коробу или частичному отбору.

Кто создает:
- ричтрак-сервис / склад

Что меняет:
- создает `WarehouseOperationTask`

### `movement_started`

Смысл:
- исполнитель реально начал перемещение.

Кто создает:
- ричтрак

Что меняет:
- переводит `WarehouseOperationTask` в активный статус;
- может проставлять `active_operation_type` в snapshot

### `movement_completed`

Смысл:
- перемещение завершено;
- товар прибыл в целевую зону.

Кто создает:
- ричтрак

Что меняет:
- обновляет `WarehouseStockSnapshot`;
- обновляет `WarehouseOperationTask` и, возможно, `WarehouseOperation`

### `movement_canceled`

Смысл:
- перемещение отменено.

Кто создает:
- склад / инициирующий контур

Что меняет:
- закрывает `WarehouseOperation` / `WarehouseOperationTask`;
- не должно терять уже зафиксированные факты

## 4. События обработки

### `processing_requested`

Смысл:
- создана потребность на обработку.

Кто создает:
- обработка

Что меняет:
- формирует processing-context;
- обычно сопровождается `processing_reserved`

### `processing_zone_arrived`

Смысл:
- товар доставлен в `OBR`.

Кто создает:
- ричтрак

Что меняет:
- обновляет зону в snapshot;
- переводит товар в `IN_PROCESSING_ZONE`

### `processing_started`

Смысл:
- обработка реально началась.

Кто создает:
- обработчик / начальник обработки

Что меняет:
- проставляет активную операцию обработки;
- переводит товар в `PROCESSING_IN_PROGRESS`

### `processing_completed`

Смысл:
- обработка завершена.

Кто создает:
- обработка

Что меняет:
- завершает processing-operation;
- может создавать новый или обновленный `WarehouseStockSnapshot`;
- может переводить товар в `PLACED_AFTER_PROCESSING` или `STORED`

## 5. События отгрузки и `OTG`

### `otg_requested`

Смысл:
- товар нужно доставить в `OTG`.

Кто создает:
- отгрузка / кладовщик

Что меняет:
- создает `WarehouseOperation` типа `move_to_otg`

### `otg_arrived`

Смысл:
- товар физически доставлен в `OTG`.

Кто создает:
- ричтрак

Что меняет:
- обновляет зону в snapshot;
- переводит товар в `IN_OTG`

### `palletization_started`

Смысл:
- начато формирование отгрузочных паллет.

Кто создает:
- кладовщик

Что меняет:
- открывает `WarehouseOperation` типа `palletization`;
- переводит товар в `PALLETIZING`

### `palletization_completed`

Смысл:
- товар собран в отгрузочные контейнеры и паллеты.

Кто создает:
- кладовщик

Что меняет:
- обновляет контейнерный состав в snapshot;
- может переводить товар в `READY_FOR_LOADING`

### `ready_for_loading`

Смысл:
- склад завершил подготовку товара к погрузке.

Кто создает:
- склад / кладовщик

Что меняет:
- переводит товар в `READY_FOR_LOADING`

## 6. События логистики и выбытия

### `assigned_to_trip`

Смысл:
- товар или отгрузка назначены на конкретный рейс.

Кто создает:
- логистика

Что меняет:
- связывает товар с trip-context;
- может переводить товар в `ASSIGNED_TO_TRIP`

### `loading_started`

Смысл:
- погрузка в машину реально началась.

Кто создает:
- логистика / склад

Что меняет:
- открывает `WarehouseOperation` типа `load_to_vehicle`;
- переводит товар в `LOADING_IN_PROGRESS`

### `loaded_to_vehicle`

Смысл:
- товар физически загружен в машину.

Кто создает:
- логистика / склад

Что меняет:
- обновляет snapshot;
- переводит товар в `LOADED_TO_VEHICLE`

### `shipped`

Смысл:
- товар выбыл со склада.

Кто создает:
- логистика / отгрузка

Что меняет:
- завершает складской жизненный цикл;
- переводит товар в `SHIPPED`;
- убирает товар из активного складского остатка

## 7. События отмены и отката

### `warehouse_context_canceled`

Смысл:
- складской контекст отменен целиком.

Примеры:
- отменена приемка;
- отменена обработка;
- отменена отгрузка до физического выбытия.

Что меняет:
- закрывает активные резервы;
- закрывает активные операции;
- переводит контекст в `CANCELED`

### `stock_returned_to_storage`

Смысл:
- товар возвращен обратно в хранение после промежуточного процесса.

Примеры:
- возврат из `OBR`;
- возврат из `OTG`;
- отмененная погрузка.

Что меняет:
- обновляет зону и доступность;
- снимает активную промежуточную операцию

## Как события обновляют текущий складской снимок

Ниже главное правило, ради которого мы вообще вводим словарь событий.

### События, которые обычно обновляют snapshot напрямую

- `placement_completed`
- `putaway_completed`
- `movement_completed`
- `processing_zone_arrived`
- `processing_completed`
- `otg_arrived`
- `palletization_completed`
- `loaded_to_vehicle`
- `stock_returned_to_storage`

Эти события меняют:

- `zone`
- `location`
- `box_code`
- `pallet_code`
- `qty`
- `available_qty`
- `active_operation_type`
- `warehouse_state_code`

### События, которые обычно обновляют reserve-layer

- `processing_reserved`
- `processing_reserve_released`
- `shipping_reserved`
- `shipping_reserve_released`

Они меняют:

- `WarehouseReserve`
- материализованную доступность и резервы в snapshot

### События, которые обычно обновляют operation-layer

- `putaway_requested`
- `movement_requested`
- `movement_task_created`
- `movement_started`
- `movement_completed`
- `movement_canceled`
- `palletization_started`
- `loading_started`

Они меняют:

- `WarehouseOperation`
- `WarehouseOperationTask`
- активную операцию в snapshot

## Сводная таблица

| Событие | Кто создает | Что обновляет в первую очередь | Что меняется в текущем состоянии |
| --- | --- | --- | --- |
| `receiving_arrived` | приемка | warehouse-context | товар признан поступившим |
| `placement_completed` | приемка / кладовщик | snapshot | появляется физический складской факт |
| `putaway_requested` | приемка | operation | создается операция перемещения в хранение |
| `putaway_completed` | ричтрак | snapshot | товар попадает в хранение |
| `processing_reserved` | обработка | reserve | уменьшается доступность под свободные операции |
| `movement_requested` | приемка / обработка / отгрузка | operation | появляется складская операция перемещения |
| `movement_started` | ричтрак | operation/task | появляется активное перемещение |
| `processing_zone_arrived` | ричтрак | snapshot | товар оказывается в `OBR` |
| `processing_started` | обработка | snapshot / operation | товар считается в активной обработке |
| `processing_completed` | обработка | snapshot | возникает новый итог обработки |
| `shipping_reserved` | отгрузка | reserve | уменьшается доступность под отгрузку |
| `otg_requested` | отгрузка | operation | создается операция доставки в `OTG` |
| `otg_arrived` | ричтрак | snapshot | товар оказывается в `OTG` |
| `palletization_completed` | кладовщик | snapshot | товар собран в отгрузочные паллеты |
| `ready_for_loading` | склад | snapshot | товар готов к погрузке |
| `assigned_to_trip` | логистика | context / snapshot | товар закреплен за рейсом |
| `loading_started` | логистика / склад | operation | начинается погрузка |
| `loaded_to_vehicle` | логистика / склад | snapshot | товар физически в машине |
| `shipped` | логистика / отгрузка | snapshot | товар выбыл со склада |

## Что это означает practically

Если мы хотим потом просто “смотреть в базу и видеть текущее состояние”, то:

1. нельзя хранить только события без snapshot;
2. нельзя хранить только snapshot без событий;
3. нельзя оставлять складские факты только в UI-логике и payload'ах документов.

Правильная модель:

- операции и действия порождают `WarehouseEvent`;
- события обновляют `WarehouseReserve`, `WarehouseOperation` и `WarehouseStockSnapshot`;
- все экраны читают уже materialized snapshot и, при необходимости, активную операцию.

## Следующий шаг

Следующий документ должен зафиксировать transition map:

- какие warehouse-state существуют;
- какие события переводят товар из одного состояния в другое;
- какие переходы допустимы, а какие нет.

После этого уже можно будет проектировать первый кодовый write-path, а не только слой чтения.

Карта переходов зафиксирована в:

- [WAREHOUSE_TRANSITION_MAP.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_TRANSITION_MAP.md)
