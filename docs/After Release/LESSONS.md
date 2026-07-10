# Lessons Learned

## Technical Insights
1. **Django Channels & WebSockets**: Configuring Channel layers with Redis required precise configuration of settings.py (`CHANNEL_LAYERS`). Scaling websockets requires a robust Redis instances configuration.
2. **Third-party Verification**: Relying on Firebase for SMS and Instagram OAuth requires managing external credentials carefully in the environment file (`.env`).
3. **MVT & Frontend Integration**: Keeping frontend state synced with backend model transactions (such as task claims and points updates) is cleaner when updates are pushed directly over WebSockets or handled through API-like views.