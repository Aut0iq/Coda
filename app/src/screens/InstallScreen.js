// Ход установки: этапы из ::step, журнал, предупреждения, итог и проверка «телефон → сервер».
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, ScrollView, StyleSheet, Text, View } from 'react-native';
import * as Clipboard from 'expo-clipboard';
import { C } from '../theme';
import { Banner, Btn, Card, Muted, Title } from '../components/ui';
import { explainError } from '../protocol';
import * as deploy from '../deploy';
import { saveServer, loadSecrets, newId } from '../storage';
import { checkReachable } from '../api';

const LOG_KEEP = 400;

export default function InstallScreen({ ctx, existing, onDone, onBack, onEdit }) {
  const { session, form, fingerprint, kv } = ctx;
  // после обрыва связи установщик переподключается: актуальный SSH-сеанс всегда в sref.current
  const sref = useRef(session);
  const [phase, setPhase] = useState('running');     // running | error | verifying | done | unreachable
  const [steps, setSteps] = useState([]);
  const [log, setLog] = useState([]);
  const [warns, setWarns] = useState([]);
  const [err, setErr] = useState(null);
  const [server, setServer] = useState(null);
  const [reach, setReach] = useState(null);
  const logRef = useRef([]);
  const started = useRef(false);

  const pushLog = (text) => {
    logRef.current = [...logRef.current, text].slice(-LOG_KEEP);
    setLog(logRef.current);
  };

  const onEvent = useCallback((ev) => {
    if (ev.type === 'step') {
      setSteps((prev) => [...prev.map((s) => (s.status === 'active' ? { ...s, status: 'done' } : s)),
        { id: ev.id, label: ev.label, status: ev.id === 'done' ? 'done' : 'active' }]);
    } else if (ev.type === 'warn') {
      setWarns((w) => [...w, ev.text]);
      pushLog(`⚠ ${ev.text}`);
    } else if (ev.type === 'error') {
      pushLog(`✗ ${ev.text}`);
    } else if (ev.type === 'log') {
      pushLog(ev.text);
    } else {
      pushLog(ev.text);
    }
  }, []);

  const start = useCallback(async (tls = form.tls, httpPort = form.httpPort) => {
    setPhase('running'); setErr(null); setSteps([]); setWarns([]); setReach(null);
    logRef.current = []; setLog([]);
    try {
      const result = await deploy.runInstall(sref, {
        ...form, host: form.publicHost, tls, httpPort,
        conn: { host: form.host, port: form.port, user: form.user, password: form.password, fingerprint },
      }, onEvent);
      const role = result.role || (form.mode === 'remote' ? 'remote' : 'local');

      // Navidrome на другом сервере: сервер скачивания сам заходит туда по SSH (пароль — один раз, через stdin)
      if (role === 'remote' && form.remote) {
        await deploy.setupRemote(sref, { user: form.user, password: form.password, remote: form.remote, hubDir: result.dir }, onEvent);
      }

      const prev = existing ? await loadSecrets(existing.id) : null;
      const rf = form.remote;
      const sv = {
        id: existing?.id || newId(), name: form.host, host: form.host,
        sshPort: form.port, sshUser: form.user,
        url: result.url, api: result.api, tls: !!result.tls, mode: result.mode,
        httpPort, fingerprint, version: result.version, dir: result.dir,
        addedAt: existing?.addedAt || Date.now(),
        role,
        // не секреты: что показать в списке и в меню; пароли — только в SecureStore
        musicDir: role === 'local' ? (form.musicDir || '') : '',
        remote: role === 'remote'
          ? (rf ? { host: rf.host, port: rf.port, user: rf.user, musicDir: rf.musicDir } : existing?.remote || null)
          : null,
        ndUrl: role === 'remote' ? (rf ? rf.ndUrl : existing?.ndUrl || '') : result.navidrome.url,
      };
      await saveServer(sv, role === 'remote'
        ? { token: result.token, ndUser: rf ? rf.ndUser : prev?.ndUser || '', ndPass: rf ? rf.ndPass : prev?.ndPass || '' }
        : { token: result.token, ndUser: result.navidrome.user, ndPass: result.navidrome.password });
      setSteps((p) => p.map((s) => (s.status === 'active' ? { ...s, status: 'done' } : s)));
      await deploy.dropLog(sref, form);          // в журнале установки на сервере лежит токен
      setServer(sv);
      setPhase('verifying');
      const r = await checkReachable(sv.api);
      setReach(r);
      setPhase(r.ok ? 'done' : 'unreachable');
    } catch (e) {
      setSteps((prev) => prev.map((s) => (s.status === 'active' ? { ...s, status: 'error' } : s)));
      setErr(explainError(e.code, e.message));
      setPhase('error');
    }
  }, [form, existing, fingerprint, onEvent]);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    start();
  }, [start]);

  const leave = async (fn) => { await deploy.cleanup(sref.current); fn(); };
  const copyLog = () => Clipboard.setStringAsync(logRef.current.join('\n')).catch(() => {});
  const recheck = async () => {
    setPhase('verifying');
    const r = await checkReachable(server.api);
    setReach(r);
    setPhase(r.ok ? 'done' : 'unreachable');
  };

  const blockedModel = err && err.restriction ? {
    level: 'blocked', ru: kv?.country === 'RU', ip: kv?.ip || '',
    title: 'Похоже, это блокировка',
    intro: kv?.country === 'RU' ? 'Сервер находится в России — Docker и часть сервисов отсюда недоступны.' : '',
    problems: [], advice: kv?.country === 'RU' ? ['vpn', 'zapret'] : ['vpn'],
  } : null;

  const title = phase === 'done' ? 'Готово' : phase === 'error' ? 'Не получилось'
    : phase === 'unreachable' ? 'Установлено, но телефон не видит сервер' : 'Устанавливаю';

  return (
    <ScrollView contentContainerStyle={s.wrap}>
      <Title>{title}</Title>
      {phase === 'running' ? <Muted style={{ marginBottom: 12 }}>Первая установка занимает несколько минут: ставится Docker и скачиваются образы.</Muted> : null}

      <Card>
        {steps.map((st) => (
          <View key={st.id} style={s.step}>
            {st.status === 'active' ? <ActivityIndicator size="small" color={C.gold} style={{ width: 18 }} />
              : <Text style={[s.mark, { color: st.status === 'error' ? C.coral : C.green }]}>{st.status === 'error' ? '✗' : '✓'}</Text>}
            <Text style={s.stepText}>{st.label}</Text>
          </View>
        ))}
        {!steps.length ? <Muted>Подключаюсь…</Muted> : null}
      </Card>

      {warns.map((w, i) => (
        <Card key={i} style={{ borderColor: C.gold }}><Text style={s.warn}>{w}</Text></Card>
      ))}

      {phase === 'error' && err ? (
        <>
          <Card style={{ borderColor: C.coral }}>
            <Text style={s.errTitle}>{err.title}</Text>
            {err.hint ? <Text style={s.errText}>{err.hint}</Text> : null}
            {err.detail ? <Text style={[s.errText, { color: C.muted }]}>{err.detail}</Text> : null}
          </Card>
          <Banner model={blockedModel} />
          {err.offerHttp ? <Btn title="Поставить без HTTPS (порт 8080)" kind="gold" onPress={() => start('off', 8080)} style={{ marginBottom: 10 }} /> : null}
          {err.editRemote && onEdit ? (
            <Btn title="Изменить данные сервера Navidrome" kind="gold" onPress={() => onEdit(sref.current)} style={{ marginBottom: 10 }} />
          ) : null}
          <Btn title="Повторить" onPress={() => start()} style={{ marginBottom: 10 }} />
        </>
      ) : null}

      {phase === 'unreachable' && server ? (
        <>
          <Card style={{ borderColor: C.gold }}>
            <Text style={s.errTitle}>{reach?.reason || 'Нет соединения'}</Text>
            <Text style={s.errText}>
              {server.tls
                ? 'Сервер поставлен, но с телефона до него не достучаться. Чаще всего порты 443 и 80 закрыты в панели хостера (файрвол или группа безопасности) — открой их и проверь снова.'
                : server.mode === 'auto'
                  ? 'Сертификат HTTPS не выпустился: Let\'s Encrypt не достучался до сервера. Открой порты 80 и 443 в панели хостера и запусти установку ещё раз либо поставь без HTTPS.'
                  : `Порт ${server.httpPort} закрыт снаружи. Открой его в панели хостера и проверь снова.`}
            </Text>
          </Card>
          <Btn title="Проверить снова" kind="gold" onPress={recheck} style={{ marginBottom: 10 }} />
          {server.mode === 'auto' && !server.tls ? (
            <Btn title="Поставить без HTTPS (порт 8080)" onPress={() => start('off', 8080)} style={{ marginBottom: 10 }} />
          ) : null}
          <Btn title="Всё равно открыть" onPress={() => leave(() => onDone(server))} style={{ marginBottom: 10 }} />
        </>
      ) : null}

      {phase === 'verifying' ? <ActivityIndicator color={C.gold} style={{ marginVertical: 12 }} /> : null}

      {phase === 'done' && server ? (
        <>
          <Card style={{ borderColor: C.green }}>
            <Text style={s.okTitle}>Сервер работает</Text>
            <Text style={s.errText}>{server.url}</Text>
            {server.role === 'remote' ? (
              <Text style={[s.errText, { color: C.muted }]}>
                Музыка будет уходить на {server.remote ? `${server.remote.user}@${server.remote.host}` : 'сервер Navidrome'}
                {server.remote ? ` в папку ${server.remote.musicDir}` : ''}. Плеер подключай к Navidrome напрямую:
                адрес{server.ndUrl ? ` ${server.ndUrl}` : ''}, логин и пароль будут в меню приложения.
              </Text>
            ) : (
              <Text style={[s.errText, { color: C.muted }]}>
                Navidrome стоит на том же адресе: логин и пароль для плеера будут в меню приложения.
              </Text>
            )}
          </Card>
          <Btn title="Открыть" kind="gold" onPress={() => leave(() => onDone(server))} style={{ marginBottom: 10 }} />
        </>
      ) : null}

      {log.length ? (
        <Card style={{ padding: 10 }}>
          <Text style={s.logHead}>Журнал</Text>
          <Text style={s.log} selectable>{log.slice(-14).join('\n')}</Text>
        </Card>
      ) : null}

      {phase !== 'running' && phase !== 'verifying' ? (
        <>
          <Btn title="Скопировать журнал" onPress={copyLog} style={{ marginBottom: 10 }} />
          {phase !== 'done' ? <Btn title="Назад" onPress={() => leave(onBack)} /> : null}
        </>
      ) : null}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { padding: 16, paddingBottom: 48 },
  step: { flexDirection: 'row', alignItems: 'center', paddingVertical: 5 },
  mark: { width: 18, fontSize: 15, fontWeight: '700' },
  stepText: { color: C.paper, fontSize: 14, flex: 1, marginLeft: 6 },
  warn: { color: C.gold, fontSize: 13, lineHeight: 19 },
  errTitle: { color: C.paper, fontWeight: '700', fontSize: 15, marginBottom: 4 },
  okTitle: { color: C.green, fontWeight: '700', fontSize: 15, marginBottom: 4 },
  errText: { color: C.paper, fontSize: 13.5, lineHeight: 20, marginTop: 2 },
  logHead: { color: C.muted, fontSize: 11, textTransform: 'uppercase', marginBottom: 4 },
  log: { color: C.muted, fontSize: 11, lineHeight: 15, fontFamily: 'monospace' },
});
