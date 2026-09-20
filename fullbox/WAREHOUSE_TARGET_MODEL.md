# Целевая модель склада

## Назначение

Этот документ описывает целевую архитектуру склада для Fullbox.

Цель такая:

- склад становится единым источником истины по движению товара
- поступление, обработка, отгрузка, логистика, ричтрак и кабинеты читают складские факты, а не придумывают свое состояние
- статусы документов остаются на уровне бизнеса, но больше не подменяют собой состояние товара

Этот документ является следующим шагом после [WAREHOUSE_TRUTH_AUDIT.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_TRUTH_AUDIT.md).

Перед чтением этого документа полезно посмотреть и доменную карту системы:

- [WAREHOUSE_DOMAIN_MAP.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_DOMAIN_MAP.md)
- [WAREHOUSE_FACT_MATRIX.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_FACT_MATRIX.md)
- [WAREHOUSE_EVENTS_DICTIONARY.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_EVENTS_DICTIONARY.md)
- [WAREHOUSE_TRANSITION_MAP.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_TRANSITION_MAP.md)
- [WAREHOUSE_DB_SCHEMA.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_DB_SCHEMA.md)

## Базовый принцип

Нам не нужно одно универсальное поле `status`, в котором будет “все обо всем”.

Нам нужны три явных слоя:

1. складской факт
2. складская операция
3. бизнес-документ

Если эти слои разделены, система становится понятной и устойчивой.

## Слой 1. Складской факт

Складской факт отвечает на вопросы:

- где товар
- в каком он контейнере
- сколько его физически
- какой резерв на него повешен
- какие специальные складские атрибуты у него есть

### Каноническая сущность: `WarehouseStockSnapshot`

Реалистичный путь внедрения:

- развивать текущий `StockPalletState`
- не выбрасывать его сразу

Ответственность:

- одна строка описывает физически размещаемую единицу остатка или агрегированную единицу в конкретной складской точке

Канонические поля:

- `agency_id`
- `stock_unit_type`
  - item
  - box
  - pallet
  - mixed
- `source_context_type`
  - receiving
  - processing
  - shipping
  - manual
- `source_context_id`
- `sku_id`
- `sku_code`
- `size`
- `barcode`
- `goods_type`
- `marking_code`
- `qty`
- `available_qty`
- `container_box_code`
- `container_pallet_code`
- `zone`
- `row`
- `section`
- `tier`
- `cell`
- `location_label`
- `warehouse_state_code`
- `active_operation_type`
- `active_operation_id`
- `updated_at`

### Что должно стать warehouse-native внутри этой модели

- физическая локация
- количество
- принадлежность коробу или паллете
- нахождение в `PR`, `OS`, `OBR`, `OTG`, зоне погрузки, машине или вне склада
- привязка к активной складской операции

### Что не должно быть ее первичной правдой

- согласование менеджером
- подписи документов
- подтверждение клиентом
- комментарии логиста

## Слой 2. Складская операция

Складская операция отвечает на вопросы:

- какая складская работа запланирована
- что сейчас выполняется
- что уже завершено
- что двигали, откуда, куда и зачем

### Каноническая сущность: `WarehouseOperation`

Реалистичный путь внедрения:

- развивать `MoveRequest`

Ответственность:

- бизнес-нейтральное намерение склада

Канонические поля:

- `operation_type`
  - putaway
  - move_to_processing
  - move_to_otg
  - palletization
  - move_to_loading
  - load_to_vehicle
  - return_to_storage
  - internal_relocation
- `context_type`
  - receiving
  - processing
  - shipping
  - logistics
  - warehouse
- `context_id`
- `agency_id`
- `reserve_type`
  - none
  - processing
  - shipping
- `source_zone`
- `destination_zone`
- `status`
  - created
  - planned
  - in_progress
  - partial
  - done
  - blocked
  - canceled
- `requested_by`
- `requested_by_role`
- `assigned_executor_role`
- `comment`
- `created_at`
- `updated_at`

### Каноническая сущность: `WarehouseOperationTask`

Реалистичный путь внедрения:

- развивать `MoveTask`

Ответственность:

- конкретная исполнимая задача склада

Канонические поля:

- `operation_id`
- `task_type`
  - pallet_move
  - box_move
  - partial_pick
  - palletization_step
  - loading_scan
- `stock_locator_type`
  - pallet
  - box
  - virtual_group
- `pallet_code`
- `box_code`
- `from_zone`
- `from_row`
- `from_section`
- `from_tier`
- `from_cell`
- `to_zone`
- `to_row`
- `to_section`
- `to_tier`
- `to_cell`
- `qty_planned`
- `qty_done`
- `status`
- `payload`
- `assigned_to`
- `assigned_to_name`
- `started_at`
- `completed_at`

## Слой 3. Бизнес-документ

Бизнес-документы отвечают на вопросы:

- кто запросил работу
- кто согласовал
- к какому рейсу относится товар
- какой документ подписан

Эти сущности продолжают жить в своих модулях:

- заявка на приемку
- заявка на обработку
- заявка на отгрузку
- рейс
- транспортная накладная
- акты

Правило:

- документ может инициировать складскую работу
- документ может реагировать на складскую работу
- но документ не должен определять физическую правду о товаре

## Каноническая модель резервов

Сейчас резервы разделены:

- резерв обработки: `InventoryState`
- резерв отгрузки: `ShippingReserve`

Целевая модель:

### Каноническая сущность: `WarehouseReserve`

Поля:

- `agency_id`
- `reserve_type`
  - processing
  - shipping
- `context_type`
  - processing
  - shipping
- `context_id`
- `sku_id`
- `sku_code`
- `size`
- `barcode`
- `goods_type`
- `qty_reserved`
- `qty_satisfied`
- `status`
  - active
  - partially_satisfied
  - satisfied
  - canceled
- `created_by`
- `created_at`
- `updated_at`

### Почему это важно

Склад не должен по-разному считать доступность товара в зависимости от того, кто его зарезервировал.

Склад должен отвечать на три базовых вопроса:

- сколько товара физически есть
- сколько из него занято резервами
- сколько уже привязано к активному движению

### Путь миграции

Краткосрочно:

- оставить `InventoryState` и `ShippingReserve`
- рассматривать их как адаптеры
- продолжать материализовать их влияние в `StockPalletState`

Долгосрочно:

- заменить их единой сущностью `WarehouseReserve`

## Каноническая модель warehouse events

Нужны неизменяемые складские события.

Именно этого сейчас не хватает, чтобы можно было объяснить любое состояние “почему сейчас так”.

### Каноническая сущность: `WarehouseEvent`

Поля:

- `agency_id`
- `event_type`
- `context_type`
- `context_id`
- `related_operation_id`
- `related_task_id`
- `sku_code`
- `size`
- `barcode`
- `goods_type`
- `qty`
- `pallet_code`
- `box_code`
- `from_zone`
- `from_row`
- `from_section`
- `from_tier`
- `from_cell`
- `to_zone`
- `to_row`
- `to_section`
- `to_tier`
- `to_cell`
- `actor_user_id`
- `actor_role`
- `payload`
- `created_at`

### Минимальный набор событий

- `receiving_submitted`
- `receiving_approved`
- `receiving_accepted_by_storekeeper`
- `placement_closed`
- `putaway_requested`
- `putaway_started`
- `putaway_completed`
- `processing_reserved`
- `processing_move_requested`
- `processing_move_completed`
- `processing_started`
- `processing_completed`
- `processing_placement_closed`
- `marking_reserved`
- `marking_used`
- `shipping_reserved`
- `otg_move_requested`
- `otg_move_completed`
- `palletization_started`
- `palletization_completed`
- `trip_assigned`
- `loading_started`
- `loading_scan_completed`
- `loading_completed`
- `shipped`
- `reserve_released`

### Практическая рекомендация

Не надо пытаться за один шаг заменить `OrderAuditEntry`.

Правильнее так:

- оставить `OrderAuditEntry` для аудита и истории документов
- ввести warehouse events как складскую историю
- постепенно перевести восстановление текущего состояния на warehouse events и warehouse snapshot

## Канонические складские состояния

Это не “сырые статусы таблиц”, а нормализованные состояния read-model.

### Состояния жизненного цикла товара

- `received_unplaced`
- `placed_in_receiving`
- `stored`
- `reserved_for_processing`
- `moving_to_processing`
- `in_processing_zone`
- `processing_in_progress`
- `placed_after_processing`
- `reserved_for_shipping`
- `moving_to_otg`
- `in_otg`
- `palletizing`
- `ready_for_loading`
- `assigned_to_trip`
- `loading_in_progress`
- `loaded_to_vehicle`
- `shipped`

### Важное правило

Эти состояния должны вычисляться из:

- складского снимка
- активных резервов
- активных операций
- фактов погрузки

Их нельзя вручную поддерживать в каждом модуле отдельно.

## Канонические read-model

### 1. `WarehouseGoodsStateResolver`

Назначение:

- отвечать любому модулю, в каком нормализованном warehouse-state находится товар или заявка

Вход:

- id приемки
- id обработки
- id отгрузки
- id рейса
- или agency + SKU + контейнер

Выход:

- нормализованный код warehouse-state
- подписи для разных аудиторий
- сводка по резервам
- сводка по текущей локации
- сводка по активной операции

### 2. `WarehouseAvailabilityResolver`

Назначение:

- отвечать, сколько товара реально доступно после всех резервов

Вход:

- agency
- SKU / size / barcode / goods_type

Выход:

- физическое количество
- резерв обработки
- резерв отгрузки
- удержание активным перемещением
- итоговая доступность

### 3. `WarehouseMovementResolver`

Назначение:

- отвечать, какое движение сейчас происходит

Вход:

- context id
- pallet code
- box code

Выход:

- активная операция
- список задач
- статус исполнения
- последнее завершенное перемещение

## Границы ответственности

### Поступление владеет

- документом приемки
- процессом согласования
- актом приемки

Поступление **не владеет**:

- финальной физической правдой после закрытия размещения

После закрытия размещения физической правдой владеет склад.

### Обработка владеет

- документом обработки
- составом работ
- разногласиями
- processing session orchestration

Обработка **не владеет**:

- правдой о физическом движении
- финальной складской правдой после закрытия размещения

### Отгрузка владеет

- документом отгрузки
- запрошенным ассортиментом
- документами отгрузки

Отгрузка **не владеет**:

- правдой о паллетах-источниках
- правдой о доставке в OTG
- правдой о погрузке

### Логистика владеет

- планированием рейса
- порядком маршрута
- перевозчиком, машиной, водителем

Логистика **не владеет**:

- фактическим складским движением товара

### Ричтрак владеет

- исполнением задач на перемещение
- физическим подтверждением движения

### Склад владеет

- местом нахождения товара
- резервом, влияющим на товар
- активной складской операцией
- готовностью товара к следующему этапу

## Что нужно сохранить без ломки

### Оставить почти без изменения

- `StockPalletState` как основу физического snapshot
- `MoveRequest` как основу warehouse operation
- `MoveTask` как основу warehouse execution task
- `MarkingCode` как отдельную подсистему маркировки

### Оставить, но понизить до вспомогательной роли

- восстановление текущего состояния из `OrderAuditEntry.payload`
- разбросанную по модулям логику UI-статусов

### Оставить как переходные адаптеры

- `InventoryState`
- `ShippingReserve`

## Рекомендуемый план миграции

### Шаг 1. Ввести единый warehouse read-model boundary

Результат:

- один сервисный пакет, например `sklad/services/warehouse_state.py`

Ответственность:

- резолвить каноническое складское состояние
- резолвить доступность
- резолвить активные движения

Эффект:

- кабинеты перестают вычислять свои собственные версии статуса

### Шаг 2. Перевести отображение статусов на warehouse resolver

Заменить прямую сборку статусов в:

- `shipping/selectors.py`
- `logistics/views.py`
- `todo/templatetags/todo_panel.py`
- client cabinet views

Эффект:

- один факт, один источник, разные формулировки для разных ролей

### Шаг 3. Формализовать владение резервами

Результат:

- warehouse reserve abstraction

Краткосрочно:

- адаптер поверх `InventoryState` и `ShippingReserve`

Долгосрочно:

- полноценный `WarehouseReserve`

Эффект:

- доступность перестает зависеть от логики отдельных модулей

### Шаг 4. Формализовать warehouse events

Результат:

- `WarehouseEvent`

Эффект:

- можно объяснить любое движение без опоры на разрозненные audit payload'ы

### Шаг 5. Формализовать погрузку как warehouse-факт

Результат:

- отдельная операция и события погрузки

Эффект:

- “загружено в машину” становится складской правдой
- логистика только читает этот факт

### Шаг 6. Снизить зависимость от audit payload как источника current-state

Результат:

- audit остается для трассировки
- текущее состояние товара читается из складских моделей

## Привязка текущих таблиц к целевой модели

### Текущий `StockPalletState`

Целевая роль:

- `WarehouseStockSnapshot`

Оставить:

- локацию
- контейнеры
- количество
- материализованные резервы

Добавить со временем:

- нормализованный `warehouse_state_code`
- ссылку на активную операцию
- при необходимости признаки погрузки

### Текущий `MoveRequest`

Целевая роль:

- `WarehouseOperation`

Оставить:

- контекст
- destination
- статус
- данные о постановщике

Расширить:

- явным типом операции
- явным типом резерва
- явной складской семантикой

### Текущий `MoveTask`

Целевая роль:

- `WarehouseOperationTask`

Оставить:

- source / destination
- pallet identity
- qty planned / done
- executor metadata

Расширить:

- явным типом задачи
- лучшей поддержкой погрузки и паллетизации

### Текущий `InventoryState`

Целевая роль:

- временный адаптер резерва обработки

Долгосрочно:

- поглощается `WarehouseReserve`

### Текущий `ShippingReserve`

Целевая роль:

- временный адаптер резерва отгрузки

Долгосрочно:

- поглощается `WarehouseReserve`

### Текущий `OrderAuditEntry`

Целевая роль:

- аудит
- история документов
- временный мост при миграции

Долгосрочно:

- не используется как главный движок current-state для warehouse truth

## Правила для дальнейшей разработки

1. Если вопрос звучит “где товар / что с товаром”, читаем складские модели.
2. Если вопрос звучит “кто согласовал / кто подписал / кто создал”, читаем бизнес-модели.
3. UI не должен сам придумывать warehouse-state из набора разрозненных условий.
4. Любой новый warehouse transition должен писать warehouse event.
5. Любой резерв должен проходить через единый pipeline доступности.
6. Погрузка и паллетизация должны стать warehouse-операциями, а не только документным контуром.

## Ближайшая инженерная задача

Следующая практическая задача должна быть такой:

- создать единый warehouse resolver module
- определить enum нормализованных складских состояний
- отобразить на этот enum текущие факты приемки, обработки, OTG, упаковки и погрузки

Почему это лучший первый шаг:

- не требует разрушительной миграции
- сразу уменьшает расхождение статусов
- создает единую точку интеграции для следующего этапа с резервами и warehouse events

## Финальная позиция

Правильная целевая архитектура — это не:

- “отгрузка в центре”
- “обработка в центре”
- “логистика в центре”

Правильная архитектура такая:

- склад в центре, когда речь идет о правде о товаре
- остальные модули сохраняют свою бизнес-правду
- все модули читают движение товара из складских моделей

Именно такая схема остановит текущий рассинхрон и сделает систему объяснимой от поступления до отгрузки.
