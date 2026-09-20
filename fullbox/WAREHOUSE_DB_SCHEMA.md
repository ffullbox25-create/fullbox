# Целевая схема БД склада

## Назначение

Этот документ фиксирует целевую модель базы данных для склада Fullbox.

Это именно **правильная целевая схема**, а не попытка аккуратно подстроиться под текущие таблицы проекта.

Цель схемы:

- сделать склад единым источником истины по товару;
- хранить текущее состояние товара в одной складской read-model;
- хранить историю изменений как отдельный event log;
- хранить операции, задачи и резервы как отдельные сущности;
- дать всем контурам один и тот же складской язык:
  - приемка
  - обработка
  - ричтрак
  - отгрузка
  - логистика

Этот документ продолжает линию:

- [WAREHOUSE_DOMAIN_MAP.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_DOMAIN_MAP.md)
- [WAREHOUSE_FACT_MATRIX.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_FACT_MATRIX.md)
- [WAREHOUSE_EVENTS_DICTIONARY.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_EVENTS_DICTIONARY.md)
- [WAREHOUSE_TRANSITION_MAP.md](/C:/Users/user/Desktop/python/WPS%20Apex/fullbox/WAREHOUSE_TRANSITION_MAP.md)

## Главный принцип

Нельзя решать задачу склада одной таблицей `status`.

Нужны отдельные слои:

1. складской снимок текущего состояния;
2. журнал складских событий;
3. складские операции;
4. исполнимые складские задачи;
5. складские резервы;
6. сущности физического размещения и контейнеризации.

То есть правильная БД склада строится не вокруг экранов и не вокруг документов, а вокруг:

- товара как складского объекта;
- места хранения;
- контейнера;
- операции;
- события;
- текущего снимка.

## Список целевых сущностей

Ниже перечислены таблицы, которые должны быть в складском ядре.

### Обязательное ядро

- `warehouse_stock_snapshot`
- `warehouse_event`
- `warehouse_operation`
- `warehouse_operation_task`
- `warehouse_reserve`

### Физическая модель

- `warehouse_location`
- `warehouse_container`
- `warehouse_stock_unit`

### Дополнительные рабочие сущности

- `warehouse_context`
- `warehouse_stock_unit_link`

Если нужно упростить первый этап, можно начать с обязательного ядра и физической модели, а дополнительные сущности ввести позже. Но правильная полная схема включает все перечисленное.

---

## 1. `warehouse_location`

### Назначение

Хранит справочник физических точек и зон склада.

Эта таблица нужна, чтобы:

- не держать локацию как просто строку;
- валидировать реальные зоны и ячейки;
- одинаково использовать локации в snapshot, операциях и событиях.

### Основные поля

- `id`
- `warehouse_code`
- `zone_code`
- `zone_kind`
- `row_no`
- `section_no`
- `tier_no`
- `cell_no`
- `location_code`
- `display_name`
- `is_active`
- `is_pickable`
- `is_storage`
- `is_processing`
- `is_shipping`
- `is_loading`
- `created_at`
- `updated_at`

### Пример `zone_kind`

- `receiving`
- `storage`
- `processing`
- `shipping`
- `loading`
- `transit`
- `vehicle`
- `virtual`

### Ключи и ограничения

- уникальность по `warehouse_code + zone_code + row_no + section_no + tier_no + cell_no`
- индекс по `zone_code`
- индекс по `zone_kind`

### Кто пишет

- складская конфигурация;
- админка склада;
- миграции справочников.

### Кто читает

- все складские контуры.

---

## 2. `warehouse_container`

### Назначение

Хранит физические контейнеры, в которых перемещается и хранится товар.

Контейнер — это не только паллета. Это любая материальная единица размещения.

### Основные поля

- `id`
- `agency_id`
- `container_type`
- `container_code`
- `parent_container_id`
- `current_location_id`
- `status`
- `source_context_type`
- `source_context_id`
- `created_by_id`
- `created_at`
- `updated_at`

### Пример `container_type`

- `box`
- `pallet`
- `mixed_pallet`
- `processing_batch`
- `shipping_pallet`

### Пример `status`

- `active`
- `merged`
- `split`
- `archived`

### Ключи и ограничения

- уникальность по `agency_id + container_code`
- индекс по `current_location_id`
- индекс по `parent_container_id`
- индекс по `container_type`

### Кто пишет

- приемка;
- обработка;
- склад;
- ричтрак;
- отгрузка.

### Кто читает

- все контуры, которым важно понимать, где именно лежит товар.

---

## 3. `warehouse_stock_unit`

### Назначение

Хранит минимальную складскую единицу учета, из которой потом собирается текущий snapshot.

Это не обязательно “одна штука товара”. Это может быть минимальная однородная партия в конкретном контейнере и месте.

### Основные поля

- `id`
- `agency_id`
- `sku_id`
- `sku_code`
- `name`
- `size`
- `barcode`
- `goods_type`
- `marking_code`
- `qty`
- `uom`
- `container_id`
- `location_id`
- `state`
- `source_context_type`
- `source_context_id`
- `created_by_id`
- `created_at`
- `updated_at`

### Пример `state`

- `active`
- `consumed`
- `split`
- `merged`
- `shipped`
- `canceled`

### Ключи и ограничения

- индекс по `agency_id + sku_code + size + barcode + goods_type`
- индекс по `location_id`
- индекс по `container_id`
- индекс по `marking_code`
- `qty > 0` для активных единиц

### Кто пишет

- приемка;
- обработка;
- складские сервисы переразмещения;
- отгрузка при изменении контейнерного состава.

### Кто читает

- snapshot-builder;
- складские операции;
- сервисы наличия и резервов.

---

## 4. `warehouse_stock_snapshot`

### Назначение

Это главная materialized read-model склада.

Именно эту таблицу должны читать:

- кабинеты;
- селекторы;
- отчеты;
- статусные блоки;
- логика доступности товара.

То есть ответ на вопрос:

- где товар;
- сколько товара;
- сколько доступно;
- сколько в резерве;
- в какой он зоне;
- в каком контейнере;
- какая операция активна

должен получаться прежде всего отсюда.

### Основные поля

- `id`
- `agency_id`
- `stock_unit_type`
- `source_context_type`
- `source_context_id`
- `sku_id`
- `sku_code`
- `name`
- `size`
- `barcode`
- `goods_type`
- `marking_code`
- `qty`
- `available_qty`
- `processing_reserved_qty`
- `shipping_reserved_qty`
- `other_reserved_qty`
- `container_id`
- `container_code`
- `parent_container_id`
- `location_id`
- `zone_code`
- `zone_kind`
- `warehouse_state_code`
- `active_operation_id`
- `active_operation_type`
- `current_trip_id`
- `is_in_vehicle`
- `is_archived`
- `snapshot_version`
- `last_event_id`
- `updated_at`
- `created_at`

### Почему это отдельная таблица

Потому что:

- события нужны для истории;
- операции нужны для процесса;
- а текущее состояние нужно быстро и однозначно читать.

### Ключи и ограничения

- индекс по `agency_id + sku_code + size + barcode + goods_type`
- индекс по `warehouse_state_code`
- индекс по `zone_code`
- индекс по `location_id`
- индекс по `container_id`
- индекс по `active_operation_id`
- индекс по `current_trip_id`
- `available_qty >= 0`
- `processing_reserved_qty >= 0`
- `shipping_reserved_qty >= 0`
- `qty >= available_qty`

### Кто пишет

Напрямую почти никто.

Эту таблицу должен обновлять только warehouse-kernel:

- обработчик событий;
- materializer snapshot;
- сервис консолидации фактов.

### Кто читает

- все модули системы.

---

## 5. `warehouse_reserve`

### Назначение

Хранит резервы как сущность первого класса.

Это принципиально важно, потому что резерв — это не просто число в snapshot, а отдельный объект:

- у него есть контекст;
- тип;
- объем;
- степень удовлетворения;
- жизненный цикл.

### Основные поля

- `id`
- `agency_id`
- `reserve_type`
- `context_type`
- `context_id`
- `sku_id`
- `sku_code`
- `size`
- `barcode`
- `goods_type`
- `marking_code`
- `qty_reserved`
- `qty_allocated`
- `qty_satisfied`
- `status`
- `source_document_type`
- `source_document_id`
- `created_by_id`
- `released_by_id`
- `created_at`
- `updated_at`

### Пример `reserve_type`

- `processing`
- `shipping`
- `quality_hold`
- `manual`

### Пример `status`

- `active`
- `partially_allocated`
- `allocated`
- `partially_satisfied`
- `satisfied`
- `released`
- `canceled`

### Ключи и ограничения

- индекс по `agency_id + reserve_type + context_type + context_id`
- индекс по `agency_id + sku_code + size + barcode + goods_type`
- индекс по `status`
- `qty_reserved > 0`
- `qty_allocated >= 0`
- `qty_satisfied >= 0`
- `qty_allocated <= qty_reserved`
- `qty_satisfied <= qty_reserved`

### Кто пишет

- обработка;
- отгрузка;
- складские ручные операции;
- warehouse-kernel.

### Кто читает

- availability;
- scheduler операций;
- snapshot materializer;
- контуры обработки и отгрузки.

---

## 6. `warehouse_operation`

### Назначение

Хранит складскую операцию как бизнес-нейтральное намерение склада.

Операция — это не документ и не экранная задача. Это складская работа.

### Основные поля

- `id`
- `agency_id`
- `operation_type`
- `context_type`
- `context_id`
- `reserve_id`
- `source_document_type`
- `source_document_id`
- `source_location_id`
- `destination_location_id`
- `source_zone_code`
- `destination_zone_code`
- `status`
- `priority`
- `requested_by_id`
- `requested_by_role`
- `assigned_executor_role`
- `comment`
- `planned_qty`
- `done_qty`
- `started_at`
- `completed_at`
- `created_at`
- `updated_at`

### Пример `operation_type`

- `putaway`
- `move_to_processing`
- `move_to_otg`
- `palletization`
- `move_to_loading`
- `load_to_vehicle`
- `return_to_storage`
- `internal_relocation`

### Пример `status`

- `created`
- `planned`
- `in_progress`
- `partial`
- `done`
- `blocked`
- `canceled`

### Ключи и ограничения

- индекс по `context_type + context_id`
- индекс по `operation_type`
- индекс по `status`
- индекс по `agency_id + destination_zone_code`
- индекс по `reserve_id`

### Кто пишет

- приемка;
- обработка;
- отгрузка;
- логистика;
- складские сервисы.

### Кто читает

- ричтрак;
- кладовщик;
- snapshot materializer;
- планировщик задач.

---

## 7. `warehouse_operation_task`

### Назначение

Хранит конкретную исполнимую задачу склада внутри операции.

Если операция — это намерение, то задача — это уже то, что берет в работу исполнитель.

### Основные поля

- `id`
- `operation_id`
- `task_type`
- `stock_unit_id`
- `container_id`
- `from_location_id`
- `to_location_id`
- `from_zone_code`
- `to_zone_code`
- `qty_planned`
- `qty_done`
- `status`
- `assigned_to_id`
- `assigned_to_name`
- `executor_role`
- `payload`
- `started_at`
- `completed_at`
- `created_at`
- `updated_at`

### Пример `task_type`

- `pallet_move`
- `box_move`
- `partial_pick`
- `palletization_step`
- `loading_step`

### Пример `status`

- `created`
- `in_progress`
- `done`
- `failed`
- `canceled`

### Ключи и ограничения

- индекс по `operation_id`
- индекс по `status`
- индекс по `assigned_to_id`
- индекс по `container_id`
- индекс по `stock_unit_id`

### Кто пишет

- scheduler операций;
- складской task planner;
- ричтрак / кладовщик при исполнении.

### Кто читает

- кабинеты исполнителей;
- warehouse-kernel;
- сервисы контроля прогресса.

---

## 8. `warehouse_event`

### Назначение

Хранит канонический журнал складских событий.

Это не замена snapshot, а исторический слой, который нужен для:

- аудита;
- восстановления;
- расследования ошибок;
- построения snapshot;
- контроля переходов.

### Основные поля

- `id`
- `agency_id`
- `event_type`
- `stock_context_type`
- `stock_context_id`
- `stock_unit_id`
- `container_id`
- `snapshot_id`
- `operation_id`
- `operation_task_id`
- `reserve_id`
- `source_document_type`
- `source_document_id`
- `from_location_id`
- `to_location_id`
- `from_zone_code`
- `to_zone_code`
- `qty`
- `payload`
- `performed_by_id`
- `performed_by_role`
- `occurred_at`
- `created_at`

### Ключи и ограничения

- индекс по `event_type`
- индекс по `stock_context_type + stock_context_id`
- индекс по `operation_id`
- индекс по `reserve_id`
- индекс по `stock_unit_id`
- индекс по `container_id`
- индекс по `occurred_at`

### Кто пишет

- только warehouse-kernel и его сервисы.

Ни один UI-контур не должен писать события “как ему удобно”.

### Кто читает

- аудит;
- отладка;
- snapshot materializer;
- сервисы восстановления;
- расследование проблем.

---

## 9. `warehouse_context`

### Назначение

Хранит складской контекст, который объединяет несколько сущностей в один жизненный цикл.

Контекст нужен, чтобы связать:

- документ-инициатор;
- складские операции;
- резервы;
- snapshot-строки;
- события.

### Основные поля

- `id`
- `context_type`
- `context_code`
- `agency_id`
- `source_document_type`
- `source_document_id`
- `status`
- `opened_at`
- `closed_at`
- `created_at`
- `updated_at`

### Пример `context_type`

- `receiving`
- `processing`
- `shipping`
- `trip`
- `warehouse_manual`

### Зачем он нужен

Чтобы можно было легко отвечать:

- какие события относятся к этой приемке;
- какие операции относятся к этой отгрузке;
- какой текущий складской факт относится к этой обработке.

---

## 10. `warehouse_stock_unit_link`

### Назначение

Хранит связи происхождения между складскими единицами.

Это нужно для сценариев:

- split;
- merge;
- перерасфасовка;
- паллетизация;
- результат обработки;
- частичная отгрузка.

### Основные поля

- `id`
- `parent_stock_unit_id`
- `child_stock_unit_id`
- `link_type`
- `qty`
- `created_at`

### Пример `link_type`

- `split`
- `merge`
- `repack`
- `processing_result`
- `shipping_repack`

### Зачем это важно

Без этой таблицы потом будет очень трудно честно объяснить:

- из чего появилась новая паллета;
- куда делась старая;
- какой результат обработки вышел из какого исходного товара.

---

## Логические связи между таблицами

Ниже упрощенная карта связей.

### Физический слой

- `warehouse_location`
- `warehouse_container`
- `warehouse_stock_unit`

### Текущий снимок

- `warehouse_stock_snapshot`

Он ссылается на:

- `warehouse_location`
- `warehouse_container`
- `warehouse_operation`
- при необходимости на `trip`

### Процессный слой

- `warehouse_reserve`
- `warehouse_operation`
- `warehouse_operation_task`

### Исторический слой

- `warehouse_event`

### Контекстный слой

- `warehouse_context`

## Что должно читаться напрямую из базы

После внедрения этой модели вопрос:

- где товар;
- сколько товара;
- сколько доступно;
- сколько под обработкой;
- сколько под отгрузкой;
- куда товар едет;
- готов ли к погрузке;
- в машине ли он уже

должен решаться прежде всего чтением:

- `warehouse_stock_snapshot`
- при необходимости `warehouse_operation`

То есть для текущего состояния товара не нужно будет каждый раз собирать ответ из:

- audit;
- processing;
- shipping;
- reachtruck;
- logistics;
- UI-логики.

Нужный ответ должен уже лежать в складской БД.

## Что не должно читаться из snapshot

Из snapshot не должны доставаться вещи, которые являются бизнес-документами:

- кто согласовал заявку;
- кто подписал акт;
- какой текст показать клиенту;
- какие комментарии оставил логист.

Это должны хранить свои бизнес-модули.

## Инварианты всей схемы

### 1. Snapshot обновляет только warehouse-kernel

Никакие шаблоны, views и document-services не должны напрямую “рисовать” текущее складское состояние.

### 2. Event log не заменяет snapshot

История и текущее состояние — разные задачи, и для них нужны разные таблицы.

### 3. Reserve — отдельная сущность

Резерв нельзя прятать только в поле `reserved_qty`.

### 4. Operation — отдельная сущность

Нельзя путать:

- “нужно переместить”
- “создан документ”
- “есть UI-задача”

### 5. Физическая модель должна быть явной

Контейнеры, локации и складские единицы должны быть отдельными объектами.

### 6. Все контуры пишут в складской язык, а не в свои частные статусы

То есть:

- приемка;
- обработка;
- ричтрак;
- отгрузка;
- логистика

пишут факты и операции склада, а не собственные альтернативные истины.

## Что можно упростить на первом этапе

Если делать внедрение поэтапно, допустимо сначала реализовать:

1. `warehouse_location`
2. `warehouse_container`
3. `warehouse_stock_snapshot`
4. `warehouse_reserve`
5. `warehouse_operation`
6. `warehouse_operation_task`
7. `warehouse_event`

А сущности:

- `warehouse_stock_unit`
- `warehouse_context`
- `warehouse_stock_unit_link`

ввести второй волной, если хочется быстрее стартовать.

Но в полной правильной модели они все равно нужны.

## Следующий шаг

Теперь, когда целевая схема БД зафиксирована, следующий правильный шаг:

1. выбрать минимальное ядро первой реализации;
2. определить, какие таблицы создаем первой миграцией;
3. решить, что будет временным адаптером к текущим таблицам;
4. после этого уже писать код materializer'а и write-path.

То есть дальше уже можно переходить к инженерному решению:

- какие именно новые Django-модели вводим первыми;
- и в каком порядке переносим на них приемку, обработку, ричтрак, отгрузку и логистику.
