import React from 'react';
import { ActivityIndicator, Linking, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { C, R } from '../theme';
import { ADVICE_TEXT } from '../banner';

export function Btn({ title, onPress, kind = 'plain', disabled, busy, style }) {
  const gold = kind === 'gold';
  const color = gold ? C.goldInk : kind === 'bad' ? C.coral : C.paper;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || busy}
      style={({ pressed }) => [s.btn, gold && s.btnGold, kind === 'bad' && s.btnBad,
        (disabled || busy) && { opacity: 0.55 }, pressed && { opacity: 0.8 }, style]}
    >
      {busy ? <ActivityIndicator color={color} /> : <Text style={[s.btnText, { color }]}>{title}</Text>}
    </Pressable>
  );
}

export function Field({ label, hint, style, ...props }) {
  return (
    <View style={[{ marginBottom: 12 }, style]}>
      <Text style={s.label}>{label}</Text>
      <TextInput
        placeholderTextColor={C.muted}
        autoCapitalize="none"
        autoCorrect={false}
        spellCheck={false}
        {...props}
        style={s.input}
      />
      {hint ? <Text style={s.hint}>{hint}</Text> : null}
    </View>
  );
}

/** Галочка с подписью (и пояснением под ней). */
export function Check({ value, onChange, label, hint, disabled }) {
  return (
    <Pressable onPress={() => !disabled && onChange(!value)} style={[s.check, disabled && { opacity: 0.55 }]}>
      <View style={[s.box, value && s.boxOn]}>{value ? <Text style={s.tick}>✓</Text> : null}</View>
      <View style={{ flex: 1 }}>
        <Text style={s.checkLabel}>{label}</Text>
        {hint ? <Text style={s.hint}>{hint}</Text> : null}
      </View>
    </Pressable>
  );
}

export function Card({ children, style }) {
  return <View style={[s.card, style]}>{children}</View>;
}

export function Title({ children }) {
  return <Text style={s.title}>{children}</Text>;
}

export function Muted({ children, style }) {
  return <Text style={[s.muted, style]}>{children}</Text>;
}

export function Row({ ok, text }) {
  const color = ok === true ? C.green : ok === 'warn' ? C.gold : C.coral;
  return (
    <View style={s.row}>
      <View style={[s.dot, { backgroundColor: color }]} />
      <Text style={s.rowText}>{text}</Text>
    </View>
  );
}

/** Плашка про запреты (модель — banner.restrictionBanner). */
export function Banner({ model }) {
  if (!model) return null;
  const bad = model.level === 'blocked';
  const tone = bad ? C.coral : model.level === 'info' ? C.blue : C.gold;
  return (
    <View style={[s.banner, { borderColor: tone, backgroundColor: bad ? C.coralSoft : C.ink2 }]}>
      <Text style={[s.bannerTitle, { color: tone }]}>{model.title}</Text>
      {model.intro ? <Text style={s.bannerText}>{model.intro}</Text> : null}
      {model.problems.map((p) => <Text key={p} style={s.bannerText}>• {p}</Text>)}
      {model.advice.length ? <Text style={[s.bannerText, { color: C.muted, marginTop: 8 }]}>Что можно сделать:</Text> : null}
      {model.advice.map((id) => {
        const a = ADVICE_TEXT[id];
        return (
          <View key={id} style={{ marginTop: 6 }}>
            <Text style={s.bannerText}>
              <Text style={{ fontWeight: '700' }}>{a.title}. </Text>{a.text}
            </Text>
            {a.url ? (
              <Text style={s.link} onPress={() => Linking.openURL(a.url).catch(() => {})}>{a.urlLabel} →</Text>
            ) : null}
          </View>
        );
      })}
    </View>
  );
}

const s = StyleSheet.create({
  btn: { minHeight: 48, borderRadius: R, borderWidth: 1, borderColor: C.line, backgroundColor: C.ink2,
    alignItems: 'center', justifyContent: 'center', paddingHorizontal: 16 },
  btnGold: { backgroundColor: C.gold, borderColor: C.gold },
  btnBad: { borderColor: 'rgba(226,112,95,0.5)', backgroundColor: C.coralSoft },
  btnText: { fontSize: 15, fontWeight: '600' },
  label: { color: C.muted, fontSize: 12, marginBottom: 5 },
  input: { backgroundColor: C.ink2, borderColor: C.line, borderWidth: 1, borderRadius: R, color: C.paper,
    paddingHorizontal: 13, paddingVertical: 11, fontSize: 15 },
  hint: { color: C.muted, fontSize: 11.5, marginTop: 4 },
  check: { flexDirection: 'row', alignItems: 'flex-start', paddingVertical: 6 },
  box: { width: 22, height: 22, borderRadius: 6, borderWidth: 1.5, borderColor: C.muted, marginRight: 11,
    marginTop: 1, alignItems: 'center', justifyContent: 'center' },
  boxOn: { backgroundColor: C.gold, borderColor: C.gold },
  tick: { color: C.goldInk, fontSize: 14, fontWeight: '800', marginTop: -1 },
  checkLabel: { color: C.paper, fontSize: 14.5, fontWeight: '600' },
  card: { backgroundColor: C.ink2, borderColor: C.line, borderWidth: 1, borderRadius: R, padding: 13, marginBottom: 12 },
  title: { color: C.paper, fontSize: 22, fontWeight: '800', marginBottom: 6 },
  muted: { color: C.muted, fontSize: 13, lineHeight: 19 },
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 5 },
  dot: { width: 9, height: 9, borderRadius: 5, marginRight: 10 },
  rowText: { color: C.paper, fontSize: 13.5, flex: 1 },
  banner: { borderWidth: 1, borderRadius: R, padding: 13, marginBottom: 12 },
  bannerTitle: { fontSize: 14.5, fontWeight: '700', marginBottom: 4 },
  bannerText: { color: C.paper, fontSize: 13, lineHeight: 19, marginTop: 3 },
  link: { color: C.blue, fontSize: 13, marginTop: 3, textDecorationLine: 'underline' },
});
