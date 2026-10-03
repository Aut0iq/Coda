import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, BackHandler, View } from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';
import { C } from './src/theme';
import { getLast, listServers, loadSecrets, removeServer, setLast } from './src/storage';
import ServersScreen from './src/screens/ServersScreen';
import ConnectScreen from './src/screens/ConnectScreen';
import InstallScreen from './src/screens/InstallScreen';
import HubScreen from './src/screens/HubScreen';

// Маршруты: servers → connect → install → hub. Своя мини-навигация без библиотек:
// экранов четыре, а меньше зависимостей — меньше шансов на несовпадение версий.
function Root() {
  const insets = useSafeAreaInsets();
  const [servers, setServers] = useState([]);
  const [route, setRoute] = useState({ name: 'boot' });
  const backRef = useRef(null);
  const registerBack = useCallback((fn) => { backRef.current = fn; }, []);

  const reload = useCallback(async () => {
    const list = await listServers();
    setServers(list);
    return list;
  }, []);

  const openHub = useCallback(async (server) => {
    const secrets = await loadSecrets(server.id);
    if (!secrets) {
      Alert.alert('Нет сохранённого доступа', 'Токен сервера не найден на этом телефоне — подключись заново.');
      setRoute({ name: 'connect', initial: server });
      return;
    }
    await setLast(server.id);
    setRoute({ name: 'hub', server, secrets });
  }, []);

  useEffect(() => {
    (async () => {
      const list = await reload();
      const last = await getLast();
      const target = list.find((s) => s.id === last) || (list.length === 1 ? list[0] : null);
      if (target) await openHub(target); else setRoute({ name: 'servers' });
    })();
  }, [reload, openHub]);

  useEffect(() => {
    const sub = BackHandler.addEventListener('hardwareBackPress', () => {
      if (backRef.current && backRef.current()) return true;
      if (route.name === 'install') return true;         // установку случайным «назад» не обрываем
      if (route.name === 'connect' || route.name === 'hub') { setRoute({ name: 'servers' }); return true; }
      return false;
    });
    return () => sub.remove();
  }, [route]);

  const toServers = useCallback(async () => { await reload(); setRoute({ name: 'servers' }); }, [reload]);

  let screen = null;
  switch (route.name) {
    case 'servers':
      screen = <ServersScreen servers={servers} onOpen={openHub} onAdd={() => setRoute({ name: 'connect' })} />;
      break;
    case 'connect':
      screen = (
        <ConnectScreen
          initial={route.initial}
          draft={route.draft}
          onCancel={toServers}
          onReady={(ctx, draft) => setRoute({ name: 'install', ctx, existing: route.initial, draft })}
        />
      );
      break;
    case 'install':
      screen = (
        <InstallScreen
          ctx={route.ctx}
          existing={route.existing}
          onBack={toServers}
          // исправить данные сервера Navidrome: форма и открытый SSH-сеанс возвращаются как были
          onEdit={(session) => setRoute({
            name: 'connect', initial: route.existing,
            draft: { ...route.draft, check: { ...route.draft.check, session } },
          })}
          onDone={async (server) => { await reload(); await openHub(server); }}
        />
      );
      break;
    case 'hub':
      screen = (
        <HubScreen
          server={route.server}
          secrets={route.secrets}
          registerBack={registerBack}
          onExit={toServers}
          onUpdate={() => setRoute({ name: 'connect', initial: route.server })}
          onRemove={async () => { await removeServer(route.server.id); await toServers(); }}
        />
      );
      break;
    default:
      screen = null;
  }

  return (
    <View style={{ flex: 1, backgroundColor: C.ink, paddingTop: insets.top, paddingBottom: insets.bottom }}>
      <StatusBar style="light" />
      {screen}
    </View>
  );
}

export default function App() {
  return (
    <SafeAreaProvider>
      <Root />
    </SafeAreaProvider>
  );
}
