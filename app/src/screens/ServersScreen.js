import React from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { C, R } from '../theme';
import { Btn, Muted, Title } from '../components/ui';

export default function ServersScreen({ servers, onOpen, onAdd }) {
  return (
    <ScrollView contentContainerStyle={s.wrap}>
      <Title>Coda</Title>
      <Muted style={{ marginBottom: 18 }}>
        Свой сервер для скачивания музыки в Navidrome. Укажи IP, логин и пароль — приложение само всё поставит.
      </Muted>
      {servers.map((sv) => (
        <Pressable key={sv.id} onPress={() => onOpen(sv)} style={({ pressed }) => [s.card, pressed && { opacity: 0.8 }]}>
          <Text style={s.name}>{sv.name}</Text>
          <Text style={s.url}>{sv.url}</Text>
          <Text style={[s.badge, { color: sv.tls ? C.green : C.gold }]}>
            {sv.tls ? 'HTTPS' : 'без HTTPS'} · версия {sv.version || '—'}
          </Text>
          {sv.role === 'remote' && sv.remote ? (
            <Text style={s.url}>музыка → {sv.remote.user}@{sv.remote.host}:{sv.remote.musicDir}</Text>
          ) : null}
        </Pressable>
      ))}
      {!servers.length ? (
        <View style={s.empty}>
          <Text style={s.emptyText}>Серверов пока нет.</Text>
        </View>
      ) : null}
      <Btn title={servers.length ? 'Добавить ещё сервер' : 'Подключить сервер'} kind="gold" onPress={onAdd} style={{ marginTop: 8 }} />
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { padding: 16, paddingBottom: 40 },
  card: { backgroundColor: C.ink2, borderColor: C.line, borderWidth: 1, borderRadius: R, padding: 14, marginBottom: 10 },
  name: { color: C.paper, fontSize: 16, fontWeight: '700' },
  url: { color: C.muted, fontSize: 12.5, marginTop: 2 },
  badge: { fontSize: 12, marginTop: 6 },
  empty: { paddingVertical: 36, alignItems: 'center' },
  emptyText: { color: C.muted, fontSize: 14 },
});
