// Рабочий экран: мини-апп с сервера в WebView. Токен вкладываем до загрузки страницы.
import React, { useEffect, useRef, useState } from 'react';
import { Alert, Linking, Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { WebView } from 'react-native-webview';
import * as Clipboard from 'expo-clipboard';
import { C } from '../theme';
import { Btn, Card, Muted } from '../components/ui';

const originOf = (u) => (/^(https?:\/\/[^/]+)/.exec(u) || [])[1] || '';

export default function HubScreen({ server, secrets, onExit, onUpdate, onRemove, registerBack }) {
  const web = useRef(null);
  const nav = useRef({ tab: 'search', sheet: false });
  const [menu, setMenu] = useState(false);
  const [failed, setFailed] = useState(null);       // {kind:'net'|'auth', text}
  const [nonce, setNonce] = useState(0);
  const origin = originOf(server.api);
  const uri = `${server.api}/app/`;
  // Navidrome в режиме «два сервера» стоит отдельно: показываем его адрес, а не адрес сервера скачивания
  const navUrl = server.role === 'remote' ? server.ndUrl : server.url;

  useEffect(() => {
    registerBack(() => {
      if (menu) { setMenu(false); return true; }
      if (nav.current.sheet || nav.current.tab !== 'search') {
        web.current?.injectJavaScript('window.hubBack && window.hubBack(); true;');
        return true;
      }
      onExit();
      return true;
    });
    return () => registerBack(null);
  }, [menu, registerBack, onExit]);

  const onMessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.nativeEvent.data); } catch { return; }
    if (msg.type === 'nav') nav.current = { tab: msg.tab, sheet: !!msg.sheet };
    else if (msg.type === 'unauthorized') setFailed({ kind: 'auth', text: 'Сервер не принимает сохранённый токен. Возможно, его переустановили.' });
    else if (msg.type === 'open' && /^https?:\/\//.test(msg.url)) Linking.openURL(msg.url).catch(() => {});
  };

  const copy = async (label, value) => {
    await Clipboard.setStringAsync(value);
    Alert.alert('Скопировано', label);
  };

  // vici сам сохраняет адрес и токен, когда его открывают по этой ссылке
  const connectVici = () => {
    const link = `vici://coda?url=${encodeURIComponent(server.api)}&token=${encodeURIComponent(secrets.token)}`;
    Linking.openURL(link).catch(() => Alert.alert(
      'vici не открылся',
      'Похоже, vici не установлен на этом телефоне. Поставь его или впиши адрес и токен в настройках vici вручную.'));
  };

  if (failed) {
    return (
      <View style={s.fail}>
        <Text style={s.failTitle}>{failed.kind === 'auth' ? 'Нет доступа' : 'Сервер не отвечает'}</Text>
        <Muted style={{ textAlign: 'center', marginBottom: 18 }}>{failed.text}</Muted>
        <Btn title="Повторить" kind="gold" onPress={() => { setFailed(null); setNonce((n) => n + 1); }} style={{ alignSelf: 'stretch', marginBottom: 10 }} />
        <Btn title="Переустановить / обновить сервер" onPress={onUpdate} style={{ alignSelf: 'stretch', marginBottom: 10 }} />
        <Btn title="К списку серверов" onPress={onExit} style={{ alignSelf: 'stretch' }} />
      </View>
    );
  }

  return (
    <View style={{ flex: 1 }}>
      <View style={s.bar}>
        <Pressable onPress={onExit} hitSlop={10}><Text style={s.barBtn}>‹ Серверы</Text></Pressable>
        <Text style={s.barTitle} numberOfLines={1}>{server.name}</Text>
        <Pressable onPress={() => setMenu(true)} hitSlop={10}><Text style={s.barBtn}>Меню</Text></Pressable>
      </View>

      <WebView
        key={nonce}
        ref={web}
        source={{ uri }}
        style={{ flex: 1, backgroundColor: C.ink }}
        injectedJavaScriptBeforeContentLoaded={`window.HUB_TOKEN = ${JSON.stringify(secrets.token)}; true;`}
        onMessage={onMessage}
        javaScriptEnabled
        domStorageEnabled
        setSupportMultipleWindows={false}
        originWhitelist={[origin]}
        onShouldStartLoadWithRequest={(req) => {
          if (req.url.startsWith(origin) || req.url === 'about:blank') return true;
          if (/^https?:\/\//.test(req.url)) Linking.openURL(req.url).catch(() => {});
          return false;
        }}
        onError={() => setFailed({ kind: 'net', text: 'Не получается открыть сервер. Проверь интернет и что сервер включён.' })}
        onHttpError={(e) => {
          if (e.nativeEvent.statusCode >= 500) setFailed({ kind: 'net', text: `Сервер ответил ошибкой ${e.nativeEvent.statusCode}.` });
        }}
      />

      <Modal visible={menu} animationType="slide" transparent onRequestClose={() => setMenu(false)}>
        <Pressable style={s.scrim} onPress={() => setMenu(false)} />
        <ScrollView style={s.sheet} contentContainerStyle={{ padding: 16, paddingBottom: 32 }}>
          <Text style={s.h}>Navidrome для плеера</Text>
          <Muted style={{ marginBottom: 10 }}>
            Впиши это в vici (или любой Subsonic-клиент): адрес сервера, логин и пароль.
            {server.role === 'remote' ? ' Navidrome стоит на другом сервере — плеер подключается к нему напрямую.' : ''}
          </Muted>
          {navUrl ? (
            <Card>
              <Line label="Адрес" value={navUrl} onCopy={() => copy('Адрес', navUrl)} />
              <Line label="Логин" value={secrets.ndUser} onCopy={() => copy('Логин', secrets.ndUser)} />
              <Line label="Пароль" value="••••••••••••" onCopy={() => copy('Пароль', secrets.ndPass)} />
            </Card>
          ) : (
            <Card><Muted>Адрес Navidrome не указан: плеер подключай по адресу своего сервера Navidrome.</Muted></Card>
          )}
          {server.role === 'remote' && server.remote ? (
            <Muted style={{ marginBottom: 12 }}>
              Музыка уходит на {server.remote.user}@{server.remote.host} в папку {server.remote.musicDir}.
            </Muted>
          ) : null}
          <Text style={s.h}>Поиск в vici</Text>
          <Muted style={{ marginBottom: 10 }}>
            vici сможет искать здесь песни, которых ещё нет в библиотеке, и ставить их на скачивание прямо из поиска.
            На компьютере впиши адрес и токен в настройках vici вручную.
          </Muted>
          <Card>
            <Line label="Адрес Coda" value={server.api} onCopy={() => copy('Адрес', server.api)} />
            <Line label="Токен" value="••••••••••••" onCopy={() => copy('Токен', secrets.token)} />
          </Card>
          <Btn title="Подключить vici" kind="gold" onPress={connectVici} style={{ marginBottom: 18 }} />
          <Btn title="Обновить сервер" onPress={() => { setMenu(false); onUpdate(); }} style={{ marginBottom: 10 }} />
          <Btn title="Убрать из приложения" kind="bad" onPress={() => Alert.alert(
            'Убрать сервер из приложения?',
            'На самом сервере ничего не удалится: музыка и Navidrome останутся. Чтобы подключиться снова, понадобится переустановка.',
            [{ text: 'Отмена', style: 'cancel' }, { text: 'Убрать', style: 'destructive', onPress: onRemove }])} />
        </ScrollView>
      </Modal>
    </View>
  );
}

function Line({ label, value, onCopy }) {
  return (
    <View style={s.line}>
      <View style={{ flex: 1 }}>
        <Text style={s.lineLabel}>{label}</Text>
        <Text style={s.lineValue} numberOfLines={1}>{value}</Text>
      </View>
      <Pressable onPress={onCopy} hitSlop={8}><Text style={s.copy}>Копировать</Text></Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  bar: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 14, paddingVertical: 10,
    borderBottomColor: C.line, borderBottomWidth: 1, backgroundColor: C.ink },
  barBtn: { color: C.gold, fontSize: 14, fontWeight: '600' },
  barTitle: { flex: 1, color: C.paper, fontSize: 14, textAlign: 'center', marginHorizontal: 8 },
  fail: { flex: 1, padding: 24, justifyContent: 'center' },
  failTitle: { color: C.paper, fontSize: 20, fontWeight: '800', textAlign: 'center', marginBottom: 8 },
  scrim: { flex: 1, backgroundColor: 'rgba(0,0,0,0.5)' },
  sheet: { maxHeight: '75%', backgroundColor: C.ink, borderTopLeftRadius: 16, borderTopRightRadius: 16,
    borderColor: C.line, borderWidth: 1 },
  h: { color: C.paper, fontSize: 17, fontWeight: '700', marginBottom: 4 },
  line: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8 },
  lineLabel: { color: C.muted, fontSize: 11.5 },
  lineValue: { color: C.paper, fontSize: 14.5, marginTop: 1 },
  copy: { color: C.blue, fontSize: 13, marginLeft: 10 },
});
