"""Backfill for v2: default community for everyone, opened_at = created_at for old tasks."""
from django.db import migrations
from django.db.models import F


def forwards(apps, schema_editor):
    Community = apps.get_model('basic', 'Community')
    UserProfile = apps.get_model('basic', 'UserProfile')
    Task = apps.get_model('basic', 'Task')
    public, _ = Community.objects.get_or_create(slug='public', defaults={'name': 'Public'})
    UserProfile.objects.filter(community__isnull=True).update(community=public)
    Task.objects.filter(community__isnull=True).update(community=public)
    Task.objects.update(opened_at=F('created_at'))


class Migration(migrations.Migration):
    dependencies = [('basic', '0002_v2_domain')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
