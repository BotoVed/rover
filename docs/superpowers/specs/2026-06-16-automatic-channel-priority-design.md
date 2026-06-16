# Automatic Channel Priority — Design Spec

**Дата:** 2026-06-16
**Статус:** черновик
**Репозиторий:** `BotoVed/Rover-App`

## 1. Проблема

Сейчас на Android одновременно активны BLE и TCP-интерфейсы. Reticulum сам выбирает, через какой интерфейс отправлять данные. Пользователь не может контролировать, какой транспорт используется — телефон может ходить через BLE даже когда WiFi доступен (медленнее, меньше дальность). При reconnect нет гарантии, что подключится лучший из доступных каналов.

## 2. Решение

Автоматический выбор канала по приоритету:

```
WiFi (TCP) > 4G (TCP) > BLE > LoRa
```

Система держит **только один интерфейс активным** — наилучший из доступных. Если текущий канал пропадает — переключается на следующий по приоритету. Периодически проверяет, не появился ли более приоритетный канал.

## 3. Приоритет каналов

| Приоритет | Канал | Интерфейс | Условие |
|-----------|-------|-----------|---------|
| 1 (высший) | WiFi | TCPClientInterface | TCP online + `WifiChecker.isWifiConnected()` |
| 2 | 4G | TCPClientInterface | TCP online + !isWifiConnected |
| 3 | BLE | BLEInterface | BLE interface online |
| 4 (низший) | LoRa | — | Ничего не доступно |

## 4. Компоненты (Android, чистый Kotlin)

### 4.1. `ChannelController` (новый класс)

Оркестрирует переключение каналов.

**Входные зависимости:**
- `Reticulum` instance (для detach/attach)
- `TCPClientInterface` reference
- `BLEInterface` reference
- `host`/`port` для TCP
- `ConnectivityManager` для network callback'ов
- `Context` для WifiChecker

**API:**
```kotlin
class ChannelController(
    private val rnsRef: () -> Reticulum?,
    private val tcpRef: () -> TCPClientInterface?,
    private val bleRef: () -> BLEInterface?,
    private val host: String,
    private val port: Int,
    private val context: Context,
    private val onChannelChanged: (String) -> Unit,  // "WiFi" | "4G" | "BLE" | "LoRa"
) {
    sealed class Channel {
        object WiFi : Channel()
        object Mobile4G : Channel()
        object BLE : Channel()
        object LoRa : Channel()
    }
}
```

### 4.2. Методы

#### `start()`
- Получает current channel из `ConnectivityManager` + probe TCP
- Вызывает `switchTo(best)` — detach всех, attach одного
- Запускает reactive listener и periodic probe

#### `switchTo(channel: Channel)`
- Обновляет internal state
- Detach всех, кроме `channel` (через `Transport.deregisterInterface()` + `iface.detach()`)
- Создаёт **новый экземпляр** интерфейса для `channel` (Reticulum interfaces не поддерживают reuse после `detach()` — `ioScope` cancelled)
- Attach: `iface.start()` → `rns.addInterface(iface)` → `Transport.registerInterface(iface.toRef())`
- Вызывает `onChannelChanged(channel)``

#### `probeTcp(): Boolean`
- Прямой `Socket(host, port)` с таймаутом 5с (никакого Reticulum — лёгкая проверка, не оставляет состояния)
- Если успешен → `close()` и `return true`
- Если timeout/refused → `return false`
- Switch после успешного probe создаёт **новый** `TCPClientInterface("rover-tcp", host, port)` → start → addInterface → registerInterface

#### `onNetworkChanged(type: NetworkType, available: Boolean)`
- Callback от ConnectivityManager
- Если WiFi стал доступен и current != WiFi → `probeTcp()` → switch
- Если WiFi пропал и current == WiFi → немедленный switch к BLE (или LoRa)

#### `periodicProbe()`
- Запускается в отдельной coroutine каждые 60с
- Если current ниже WiFi/4G (BLE или LoRa) → `probeTcp()` → если успех → switch

### 4.3. Интеграция в RnsManager

```kotlin
class RnsManager {
    private var channelController: ChannelController? = null
    private var currentChannel: String = "LoRa"

    fun start(identity: Identity) {
        // ... BLE init ...
        // ... LXMRouter init ...

        channelController = ChannelController(
            rnsRef = { reticulum },
            tcpRef = { tcpInterface },
            bleRef = { bleInterface },
            host = serverHost,
            port = serverPort,
            context = context,
            onChannelChanged = { channel ->
                currentChannel = channel
                // announce new channel
                deliveryDestination?.announce()
            }
        )
        channelController?.start()
    }

    suspend fun addTcpInterfaceAndWait(...) {
        // Упрощается — ChannelController сам решает, когда нужен TCP
    }

    fun getActiveChannel(): String = currentChannel
}
```

### 4.4. Reactive listener (ConnectivityManager)

```kotlin
val networkCallback = object : ConnectivityManager.NetworkCallback() {
    override fun onAvailable(network: Network) {
        val caps = cm.getNetworkCapabilities(network)
        when {
            caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true ->
                channelController?.onNetworkChanged(WIFI, available = true)
            caps?.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) == true ->
                channelController?.onNetworkChanged(CELLULAR, available = true)
        }
    }
    override fun onLost(network: Network) {
        // WiFi пропал → TCP упадёт → fallback
    }
}
cm.registerNetworkCallback(NetworkRequest.Builder().build(), networkCallback)
```

### 4.5. Periodic probe coroutine

```kotlin
fun startPeriodicProbe(scope: CoroutineScope) {
    scope.launch {
        while (true) {
            delay(60_000)
            if (currentChannel in listOf("BLE", "LoRa")) {
                if (probeTcp()) {
                    switchTo(determineBestTcpChannel())
                }
            }
        }
    }
}
```

## 5. UI (DashboardTopBar)

Изменений нет. Индикатор остаётся read-only. Значение `channel` обновляется автоматически через `onChannelChanged` → `_channel.value = channel`.

## 6. Бэкенд (Rover HA integration)

Изменений не требуется. Бэкенд слушает TCP. PONG эхо-канал клиента не меняется — клиент сам решает, что писать в `channel`.

## 7. Taблица переходов

| Было | Событие | Стало |
|------|---------|-------|
| BLE | WiFi появился → probe OK | WiFi |
| BLE | WiFi появился → probe timeout | BLE (probe повторится через 60с) |
| WiFi | WiFi пропал → TCP offline → BLE доступен | BLE |
| WiFi | WiFi пропал → TCP offline → BLE недоступен | LoRa |
| LoRa | Periodic probe: TCP OK | WiFi/4G |
| 4G | Periodic probe: TCP OK + WiFi connected | WiFi |

## 8. Edge cases

- **Нет интернета, WiFi в другой подсети**: `probeTcp()` timeout → остаёмся на BLE. На следующем probe повторим.
- **Переключение между WiFi и 4G**: TCP сам переподключается к серверу при смене сети, но мы детектим `onAvailable` и принудительно probe'им — если TCP умер, пересоздаём.
- **Одновременный WiFi + 4G**: приоритет WiFi, probe вернёт успех по WiFi.
- **BLE включается/выключается на телефоне**: BLEInterface.online меняется → при switch проверяем online.

## 9. Тестирование

1. **WiFi → BLE**: Выключить WiFi на телефоне → TCP падает → BLE подхватывает (3-10с).
2. **BLE → WiFi**: Включить WiFi → на следующем probe (~60с) переключается на WiFi.
3. **Reconnect**: Принудительный reconnect (кнопка в настройках) → probe TCP → лучший канал.
4. **4G**: Отключить WiFi, оставить мобильные данные → TCP должен подняться по 4G → канал = "4G".

## 10. Что НЕ входит (scope cut)

- **Ручное переключение по tap** — убрано по решению пользователя (автоматика вместо ручного).
- **Настройка приоритета** — жёсткий порядок WiFi > 4G > BLE > LoRa.
- **LoRa-аппаратная поддержка** — только статус, никакого LoRa-интерфейса.
- **Бэкенд** — не требует изменений.
