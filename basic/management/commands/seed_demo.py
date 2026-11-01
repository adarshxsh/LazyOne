"""Local demo data: a small friend graph, tasks in every wave and a dispute with a jury."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from basic.models import Community, Friendship, Task, UserProfile
from basic.services import disputes, marketplace

PASSWORD = 'lazyone123'


class Command(BaseCommand):
    help = f"Create demo users (password '{PASSWORD}'), friendships, tasks and a dispute. Local use only."

    def handle(self, *args, **opts):
        from django.conf import settings
        if not settings.DEBUG:
            self.stderr.write("Refusing to seed: DEBUG is off.")
            return
        if settings.REALTIME_ENABLED and settings.FIREBASE_SERVICE_ACCOUNT_KEY_PATH:
            self.stderr.write("Refusing to seed while connected to a real Firebase project. "
                              "Set REALTIME_ENABLED=False in .env for local demo data.")
            return
        community = Community.default()
        names = ['asha', 'bala', 'chen', 'dev', 'esha', 'farah', 'gopal', 'hina', 'ira', 'jay', 'kiran', 'leo']
        users = {}
        for n in names:
            u, created = User.objects.get_or_create(username=n, defaults={'email': f'{n}@example.com'})
            if created:
                u.set_password(PASSWORD)
                u.save()
                UserProfile.objects.create(user=u, rewards=1500, community=community)
                from allauth.account.models import EmailAddress
                EmailAddress.objects.get_or_create(user=u, email=u.email, defaults={'verified': True, 'primary': True})
            users[n] = u

        def friends(a, b, closeness):
            pa, pb = users[a].userprofile, users[b].userprofile
            Friendship.objects.get_or_create(from_user=pa, to_user=pb, defaults={'closeness': closeness})
            Friendship.objects.get_or_create(from_user=pb, to_user=pa, defaults={'closeness': closeness})

        friends('asha', 'bala', 90)   # wave 0 for asha's tasks
        friends('asha', 'chen', 50)   # wave 1
        friends('asha', 'dev', 15)    # wave 2
        friends('bala', 'esha', 70)   # esha is friend-of-friend -> wave 2

        deadline = timezone.now() + timedelta(days=2)
        if not Task.objects.filter(posted_by=users['asha']).exists():
            marketplace.post_task(users['asha'], title='Get graph paper from stationery', description='2 sheets, A4',
                                  reward=30, deadline=deadline)
            marketplace.post_task(users['asha'], title='Return library book', description='Due today',
                                  reward=50, deadline=deadline, wave_policy='friends_only')
            marketplace.post_task(users['farah'], title='Print assignment (10 pages)', description='',
                                  reward=40, deadline=deadline, wave_policy='community_now')
            t = marketplace.post_task(users['gopal'], title='Bring dinner from mess', description='',
                                      reward=100, deadline=deadline, wave_policy='community_now')
            marketplace.apply(t, 'accept', users['hina'])
            marketplace.apply(t, 'submit', users['hina'], note='Left it at your door')
            d = disputes.raise_dispute(t, users['gopal'], 'Nothing was at my door.', 'quality')
            disputes.add_evidence(d, users['hina'], 'Photo sent in chat at 8:10pm.')
            jurors = ', '.join(d.jurors.values_list('juror__username', flat=True))
            self.stdout.write(f"Dispute {d.public_id} jurors: {jurors}")

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(users)} users (password '{PASSWORD}'). Log in as asha/bala/chen/dev to see waves."))
