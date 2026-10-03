# JSch создаёт алгоритмы по имени класса (Class.forName) — R8 не должен их вырезать.
-keep class com.jcraft.jsch.** { *; }
# Необязательные части JSch, которых нет на Android
-dontwarn com.jcraft.jsch.**
-dontwarn org.ietf.jgss.**
-dontwarn javax.security.auth.kerberos.**
-dontwarn javax.naming.**
-dontwarn java.lang.management.**
-dontwarn org.apache.logging.log4j.**
-dontwarn org.slf4j.**
-dontwarn org.bouncycastle.**
