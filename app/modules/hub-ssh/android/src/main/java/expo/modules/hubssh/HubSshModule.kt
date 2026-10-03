package expo.modules.hubssh

import android.util.Base64
import com.jcraft.jsch.ChannelExec
import com.jcraft.jsch.HostKey
import com.jcraft.jsch.HostKeyRepository
import com.jcraft.jsch.JSch
import com.jcraft.jsch.Session
import com.jcraft.jsch.UIKeyboardInteractive
import com.jcraft.jsch.UserInfo
import expo.modules.kotlin.Promise
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.io.ByteArrayInputStream
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import kotlin.concurrent.thread

/** Отпечаток ключа сервера в том же виде, что у `ssh-keygen -lf`: SHA256:base64 без «=». */
private fun fingerprintOf(key: ByteArray): String {
  val digest = MessageDigest.getInstance("SHA-256").digest(key)
  return "SHA256:" + Base64.encodeToString(digest, Base64.NO_PADDING or Base64.NO_WRAP)
}

/**
 * Вместо known_hosts: пускает только к серверу с ожидаемым отпечатком. Если отпечатка ещё нет
 * (первое подключение), принимает любой и запоминает увиденный — приложение сохранит его себе.
 * Проверка идёт ДО отправки пароля: JSch спрашивает репозиторий сразу после обмена ключами.
 */
private class PinnedHostKey(private val expected: String?) : HostKeyRepository {
  @Volatile var seen: String? = null
  @Volatile var changed = false

  override fun check(host: String?, key: ByteArray?): Int {
    if (key == null) return HostKeyRepository.NOT_INCLUDED
    val fp = fingerprintOf(key)
    seen = fp
    if (expected.isNullOrEmpty() || expected == fp) return HostKeyRepository.OK
    changed = true
    return HostKeyRepository.CHANGED
  }

  override fun add(hostkey: HostKey?, ui: UserInfo?) {}
  override fun remove(host: String?, type: String?) {}
  override fun remove(host: String?, type: String?, key: ByteArray?) {}
  override fun getKnownHostsRepositoryID(): String = "music-hub"

  // не null: JSch перебирает результат при выборе алгоритма ключа сервера
  override fun getHostKey(): Array<HostKey> = emptyArray()
  override fun getHostKey(host: String?, type: String?): Array<HostKey> = emptyArray()
}

/** Пароль для методов `password` и `keyboard-interactive` (на Ubuntu с PAM бывает только второй). */
private class PasswordInfo(private val pw: String) : UserInfo, UIKeyboardInteractive {
  override fun getPassphrase(): String? = null
  override fun getPassword(): String = pw
  override fun promptPassword(message: String?): Boolean = true
  override fun promptPassphrase(message: String?): Boolean = false
  override fun promptYesNo(message: String?): Boolean = false
  override fun showMessage(message: String?) {}
  override fun promptKeyboardInteractive(
    destination: String?, name: String?, instruction: String?,
    prompt: Array<String>?, echo: BooleanArray?,
  ): Array<String> = Array(prompt?.size ?: 0) { pw }
}

class HubSshModule : Module() {
  private val sessions = ConcurrentHashMap<String, Session>()

  override fun definition() = ModuleDefinition {
    Name("HubSsh")

    Events("line")

    // open(id, host, port, user, password, expectedFingerprint|null) -> { fingerprint }
    AsyncFunction("open") { id: String, host: String, port: Int, user: String, password: String, expected: String?, promise: Promise ->
      thread(isDaemon = true, name = "hub-ssh-open-$id") {
        val pinned = PinnedHostKey(expected)
        var session: Session? = null
        try {
          val jsch = JSch()
          jsch.setHostKeyRepository(pinned)
          session = jsch.getSession(user, host, port)
          session.setPassword(password)
          session.setUserInfo(PasswordInfo(password))
          session.setConfig("StrictHostKeyChecking", "yes")
          session.setConfig("PreferredAuthentications", "password,keyboard-interactive")
          // установка идёт минуты, а молчащие соединения роутеры рвут
          session.setServerAliveInterval(15_000)
          session.setServerAliveCountMax(8)
          session.connect(20_000)
          sessions[id] = session
          promise.resolve(mapOf("fingerprint" to (pinned.seen ?: "")))
        } catch (e: Throwable) {
          try { session?.disconnect() } catch (_: Throwable) {}
          if (pinned.changed) {
            promise.reject(
              "HOSTKEY_CHANGED",
              "HOSTKEY_CHANGED: ключ сервера не совпадает с запомненным (${pinned.seen})",
              e,
            )
          } else {
            promise.reject("SSH_OPEN", e.message ?: e.toString(), e)
          }
        }
      }
    }

    // exec(id, execId, command, stdin|null) -> код выхода; строки вывода — событием "line"
    AsyncFunction("exec") { id: String, execId: String, command: String, stdin: String?, promise: Promise ->
      val session = sessions[id]
      if (session == null || !session.isConnected) {
        promise.reject("SSH_CLOSED", "Сеанс SSH закрыт", null)
        return@AsyncFunction
      }
      thread(isDaemon = true, name = "hub-ssh-exec-$execId") {
        var channel: ChannelExec? = null
        try {
          channel = session.openChannel("exec") as ChannelExec
          channel.setCommand(command)
          // пустой поток вместо null: удалённая команда сразу получает EOF и не ждёт ввода
          channel.setInputStream(ByteArrayInputStream((stdin ?: "").toByteArray(Charsets.UTF_8)))
          val output = channel.inputStream // до connect(), иначе начало вывода потеряется
          channel.connect(20_000)
          output.bufferedReader(Charsets.UTF_8).forEachLine { line ->
            sendEvent("line", mapOf("execId" to execId, "text" to line))
          }
          // код выхода приходит вместе с закрытием канала
          var waited = 0
          while (!channel.isClosed && waited < 100) { Thread.sleep(50); waited++ }
          promise.resolve(channel.exitStatus)
        } catch (e: Throwable) {
          promise.reject("SSH_EXEC", e.message ?: e.toString(), e)
        } finally {
          try { channel?.disconnect() } catch (_: Throwable) {}
        }
      }
    }

    AsyncFunction("close") { id: String ->
      sessions.remove(id)?.disconnect()
      true
    }

    OnDestroy {
      sessions.values.forEach { try { it.disconnect() } catch (_: Throwable) {} }
      sessions.clear()
    }
  }
}
