# The failover probe (ADR-047 §5): Probe.java run by the JDK's source launcher, with pgjdbc on
# the class path. driver.py builds it on the runner from this directory and the jar it fetched
# and checked against gates/pins.json, then loads it into kind; it is never pushed.
FROM eclipse-temurin:21.0.12_8-jdk-noble@sha256:4d271cd5e0624598cf563342f47281b09cb364bc13acbbd7251f49f83470018d
COPY postgresql.jar Probe.java /probe/
USER 65534
ENTRYPOINT ["java", "-XX:+UseSerialGC", "-Xmx128m", "-cp", "/probe/postgresql.jar", "/probe/Probe.java"]
