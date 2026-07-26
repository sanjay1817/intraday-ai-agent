"""Background scheduling (APScheduler).

Owns the process-wide `AsyncIOScheduler` instance and job registration.
Jobs call into the service layer; they never contain business logic
themselves.
"""
