# basic/routers.py

class FriendsRouter:
    """
    A router to control all database operations on models in the
    'basic' application related to friends.
    """
    route_app_labels = {'basic'}
    friend_models = {'friendship', 'friendrequest'}

    def db_for_read(self, model, **hints):
        """
        Attempts to read friend-related models from friends_db.
        """
        if model._meta.model_name in self.friend_models:
            return 'friends_db'
        return None

    def db_for_write(self, model, **hints):
        """
        Attempts to write friend-related models to friends_db.
        """
        if model._meta.model_name in self.friend_models:
            return 'friends_db'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        """
        Allow relations if a model in the friends_db is involved.
        """
        if obj1._meta.model_name in self.friend_models or \
                obj2._meta.model_name in self.friend_models:
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Make sure the friend models only appear in the 'friends_db'
        database.
        """
        if app_label == 'basic':
            if model_name in self.friend_models:
                return db == 'friends_db'
            else:
                # Ensure other 'basic' app models go to default
                return db == 'default'
        # Allow other apps to migrate on default
        elif db == 'default':
            return True
        return False
