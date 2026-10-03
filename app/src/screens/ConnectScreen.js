// Форма «IP, логин, пароль» → подключение по SSH → проверка сервера → куда класть музыку → установка.
import React, { useState } from 'react';
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, Text, View } from 'react-native';
import { C } from '../theme';
import { Banner, Btn, Card, Check, Field, Muted, Row, Title } from '../components/ui';
import { checklist, portsInfo, restrictionBanner } from '../banner';
import { isMusicDir } from '../protocol';
import * as deploy from '../deploy';
import { sshAvailable } from '../ssh';

const isUrl = (u) => /^https?:\/\/[^\s/]+/i.test(u);

/**
 * initial — сервер из списка (режим «Обновить»); draft — снимок формы, если вернулись с экрана установки
 * исправить данные сервера Navidrome (SSH-сеанс и результат проверки сохраняются, вводить заново не надо).
 */
export default function ConnectScreen({ initial, draft, onCancel, onReady }) {
  const update = !!initial;
  const d = draft || {};
  const dr = d.remote || {};
  const hadRemote = initial?.role === 'remote';

  const [host, setHost] = useState(d.host ?? initial?.host ?? '');
  const [port, setPort] = useState(d.port ?? String(initial?.sshPort || 22));
  const [user, setUser] = useState(d.user ?? initial?.sshUser ?? 'root');
  const [password, setPassword] = useState(d.password ?? '');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [check, setCheck] = useState(d.check ?? null);       // {session, fingerprint, kv}
  const [httpMode, setHttpMode] = useState(d.httpMode ?? false);
  const [httpPort, setHttpPort] = useState(d.httpPort ?? '8080');
  const [domain, setDomain] = useState(d.domain ?? '');

  // куда складывать музыку
  const [sameServer, setSameServer] = useState(d.sameServer ?? !hadRemote);
  const [musicDir, setMusicDir] = useState(d.musicDir ?? initial?.musicDir ?? '');
  const [editRemote, setEditRemote] = useState(d.editRemote ?? !hadRemote);
  const [rHost, setRHost] = useState(dr.host ?? '');
  const [rPort, setRPort] = useState(dr.port ?? '22');
  const [rUser, setRUser] = useState(dr.user ?? 'root');
  const [rPass, setRPass] = useState(dr.password ?? '');
  const [rDir, setRDir] = useState(dr.musicDir ?? '');
  const [ndUrl, setNdUrl] = useState(dr.ndUrl ?? '');
  const [ndUser, setNdUser] = useState(dr.ndUser ?? '');
  const [ndPass, setNdPass] = useState(dr.ndPass ?? '');

  const form = () => ({ host: host.trim(), port: Number(port) || 22, user: user.trim(), password });
  const remoteForm = () => ({
    host: rHost.trim(), port: Number(rPort) || 22, user: rUser.trim(), password: rPass,
    musicDir: rDir.trim(), ndUrl: ndUrl.trim().replace(/\/+$/, ''), ndUser: ndUser.trim(), ndPass,
  });
  // при обновлении сервера, уже работающего в режиме «два сервера», настройку передачи не трогаем без просьбы
  const needSetup = !sameServer && (!hadRemote || editRemote);

  async function run() {
    const f = form();
    if (!f.host || !f.user || !f.password) { setError('Заполни адрес сервера, логин и пароль'); return; }
    setError('');
    setCheck(null);
    let session = null;
    try {
      setBusy('Подключаюсь по SSH…');
      const conn = await deploy.connect({ ...f, fingerprint: initial?.fingerprint || null });
      session = conn.session;
      const fingerprint = conn.fingerprint;
      setBusy('Загружаю установщик…');
      await deploy.prepare(session, f);
      setBusy('Проверяю сервер и сеть…');
      const kv = await deploy.runPreflight(session);
      const ports = portsInfo(kv);
      if (!ports.httpsPossible) setHttpMode(true);
      if (update && initial?.mode === 'off') { setHttpMode(true); setHttpPort(String(initial.httpPort || 8080)); }
      setCheck({ session, fingerprint, kv });
    } catch (e) {
      if (session) await deploy.cleanup(session);       // не оставляем открытый SSH-сеанс после ошибки
      setError(e.message || String(e));
    } finally {
      setBusy('');
    }
  }

  /** Проверка введённого про сервер Navidrome и папку; текст ошибки или ''. */
  function validate() {
    if (sameServer) {
      if (musicDir.trim() && !isMusicDir(musicDir.trim())) {
        return 'Папка для музыки: полный путь, например /srv/music (латиница, цифры и . _ - / @ +)';
      }
      return '';
    }
    if (!needSetup) return '';
    const r = remoteForm();
    if (!r.host || !r.user || !r.password) return 'Для сервера Navidrome укажи адрес, логин и пароль SSH';
    if (!r.musicDir.startsWith('/') || /(^|\/)\.\.(\/|$)/.test(r.musicDir)) {
      return 'Папка с музыкой на сервере Navidrome — полный путь, например /srv/music';
    }
    if (r.ndUrl && !isUrl(r.ndUrl)) return 'Адрес Navidrome должен начинаться с http:// или https://';
    if (r.ndUrl && (!r.ndUser || !r.ndPass)) return 'Для адреса Navidrome нужны ещё его логин и пароль';
    return '';
  }

  const snapshot = () => ({
    host, port, user, password, httpMode, httpPort, domain, sameServer, musicDir, editRemote, check,
    remote: { host: rHost, port: rPort, user: rUser, password: rPass, musicDir: rDir, ndUrl, ndUser, ndPass },
  });

  function install() {
    const bad = validate();
    if (bad) { setError(bad); return; }
    setError('');
    const f = form();
    const tls = httpMode ? 'off' : 'auto';
    const publicHost = !httpMode && domain.trim() ? domain.trim() : f.host;
    onReady({
      session: check.session, fingerprint: check.fingerprint, kv: check.kv,
      form: {
        ...f, publicHost, tls, httpPort: Number(httpPort) || 8080,
        mode: sameServer ? 'local' : 'remote',
        musicDir: sameServer ? musicDir.trim() : '',
        remote: needSetup ? remoteForm() : null,
      },
    }, snapshot());
  }

  async function cancel() {
    if (check) await deploy.cleanup(check.session);
    onCancel();
  }

  const banner = check ? restrictionBanner(check.kv) : null;
  const ports = check ? portsInfo(check.kv) : null;
  const hardStop = check && (check.kv.kernel !== 'Linux' || !['x86_64', 'aarch64', 'arm64'].includes(check.kv.arch));
  const lockedForm = !!busy || !!check;

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={s.wrap} keyboardShouldPersistTaps="handled">
        <Title>{update ? 'Обновить сервер' : 'Новый сервер'}</Title>
        <Muted style={{ marginBottom: 16 }}>
          Сервер, на котором будет работать скачивание музыки: свежий Linux с доступом по SSH (root или
          пользователь с sudo). Пароль нигде не сохраняется: он нужен только на время установки.
        </Muted>
        {!sshAvailable ? (
          <Card><Text style={{ color: C.coral }}>В этой сборке нет SSH-модуля — установка с телефона недоступна.</Text></Card>
        ) : null}

        <Field label="IP или домен сервера" value={host} onChangeText={setHost} placeholder="203.0.113.7"
          keyboardType="url" editable={!lockedForm && !update} />
        <View style={{ flexDirection: 'row', gap: 10 }}>
          <Field style={{ flex: 1 }} label="Логин" value={user} onChangeText={setUser} editable={!lockedForm} />
          <Field style={{ width: 90 }} label="Порт SSH" value={port} onChangeText={setPort} keyboardType="number-pad" editable={!lockedForm} />
        </View>
        <Field label="Пароль" value={password} onChangeText={setPassword} secureTextEntry editable={!lockedForm} />

        {error && !check ? <Card style={{ borderColor: C.coral }}><Text style={s.err}>{error}</Text></Card> : null}

        {!check ? (
          <>
            <Btn title="Проверить сервер" kind="gold" onPress={run} busy={!!busy} disabled={!sshAvailable} />
            {busy ? <Muted style={{ textAlign: 'center', marginTop: 10 }}>{busy}</Muted> : null}
            <Btn title="Отмена" onPress={onCancel} style={{ marginTop: 10 }} disabled={!!busy} />
          </>
        ) : (
          <>
            <Banner model={banner} />

            <Card>
              {checklist(check.kv).map((r, i) => <Row key={i} ok={r.ok} text={r.text} />)}
            </Card>

            <Card>
              <Text style={s.h}>Куда складывать музыку</Text>
              <Check
                value={sameServer}
                onChange={setSameServer}
                label="Navidrome на этом же сервере"
                hint={sameServer
                  ? 'Navidrome поставится рядом, музыка останется на этом сервере.'
                  : 'Этот сервер будет только скачивать, а музыку сам отправит по SSH на сервер с Navidrome.'}
              />
              {sameServer ? (
                <Field style={{ marginTop: 8 }} label="Папка для музыки (необязательно)" value={musicDir} onChangeText={setMusicDir}
                  placeholder="/opt/music-hub/data/music"
                  hint="Полный путь на этом сервере. Если пусто — папка по умолчанию. Navidrome увидит ту же папку." />
              ) : (
                <View style={{ marginTop: 8 }}>
                  {hadRemote && initial?.remote ? (
                    <Muted style={{ marginBottom: 6 }}>
                      Сейчас: {initial.remote.user}@{initial.remote.host} → {initial.remote.musicDir}
                    </Muted>
                  ) : null}
                  {hadRemote ? (
                    <Check value={editRemote} onChange={setEditRemote} label="Изменить данные сервера Navidrome" />
                  ) : null}
                  {needSetup ? (
                    <>
                      <Muted style={{ marginBottom: 10 }}>
                        Сервер скачивания сам зайдёт на сервер Navidrome по SSH, оставит там свой ключ и дальше будет входить
                        без пароля. Нужен доступ по SSH с паролем (хотя бы на время настройки) и включённый SFTP.
                      </Muted>
                      <Field label="Адрес сервера Navidrome (IP или домен)" value={rHost} onChangeText={setRHost} placeholder="198.51.100.20" keyboardType="url" />
                      <View style={{ flexDirection: 'row', gap: 10 }}>
                        <Field style={{ flex: 1 }} label="Логин SSH" value={rUser} onChangeText={setRUser} />
                        <Field style={{ width: 90 }} label="Порт SSH" value={rPort} onChangeText={setRPort} keyboardType="number-pad" />
                      </View>
                      <Field label="Пароль SSH" value={rPass} onChangeText={setRPass} secureTextEntry
                        hint="Нужен один раз, нигде не сохраняется." />
                      <Field label="Папка с музыкой на том сервере" value={rDir} onChangeText={setRDir} placeholder="/srv/music"
                        hint="Полный путь: та же папка, что в Navidrome указана как библиотека (ND_MUSICFOLDER)." />
                      <Field label="Адрес Navidrome" value={ndUrl} onChangeText={setNdUrl} keyboardType="url"
                        placeholder={`http://${rHost.trim() || '198.51.100.20'}:4533`}
                        hint="Нужен, чтобы пропускать дубли и просить Navidrome пересканировать библиотеку. Его же увидишь в меню для плеера." />
                      <View style={{ flexDirection: 'row', gap: 10 }}>
                        <Field style={{ flex: 1 }} label="Логин Navidrome" value={ndUser} onChangeText={setNdUser} />
                        <Field style={{ flex: 1 }} label="Пароль Navidrome" value={ndPass} onChangeText={setNdPass} secureTextEntry />
                      </View>
                    </>
                  ) : null}
                </View>
              )}
            </Card>

            <Card>
              <Text style={s.h}>Как подключаться к серверу скачивания</Text>
              {ports.httpsPossible ? (
                <Muted>
                  HTTPS с настоящим сертификатом: адрес вида {check.kv.ip ? `${check.kv.ip.replace(/\./g, '-')}.sslip.io` : '…sslip.io'}.
                  Нужны свободные порты 80 и 443.
                </Muted>
              ) : (
                <Muted style={{ color: C.gold }}>
                  Порты заняты: {ports.busy.join(', ')}. HTTPS здесь не получится — поставлю без него на отдельном порту.
                </Muted>
              )}
              {ports.httpsPossible ? (
                <View style={{ marginTop: 10 }}>
                  <Row ok={!httpMode} text="HTTPS (рекомендуется)" />
                  <Btn title={httpMode ? 'Вернуть HTTPS' : 'Без HTTPS на отдельном порту'} onPress={() => setHttpMode(!httpMode)} style={{ marginTop: 6 }} />
                </View>
              ) : null}
              {httpMode ? (
                <Field style={{ marginTop: 10 }} label="Порт" value={httpPort} onChangeText={setHttpPort} keyboardType="number-pad"
                  hint={ports.port8080Busy && httpPort === '8080' ? 'Порт 8080 занят — выбери другой' : 'Без HTTPS трафик идёт открыто: токен и пароль видны в сети'} />
              ) : (
                <Field style={{ marginTop: 10 }} label="Свой домен (необязательно)" value={domain} onChangeText={setDomain}
                  placeholder="music.example.com" hint="A-запись домена должна смотреть на этот сервер" />
              )}
            </Card>

            {error ? <Card style={{ borderColor: C.coral }}><Text style={s.err}>{error}</Text></Card> : null}
            <Btn title={update ? 'Обновить' : 'Установить'} kind="gold" onPress={install} disabled={hardStop} />
            <Btn title="Отмена" onPress={cancel} style={{ marginTop: 10 }} />
          </>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  wrap: { padding: 16, paddingBottom: 48 },
  h: { color: C.paper, fontWeight: '700', fontSize: 14, marginBottom: 6 },
  err: { color: C.coral, fontSize: 13.5, lineHeight: 19 },
});
