from flask.ext.script import Manager, Server
from flask_migrate import Migrate, MigrateCommand
from app import create_app, db
from credstash import getAllSecrets
import os

secrets = getAllSecrets(region="eu-west-1")
for key, val in secrets.items():
    os.environ[key] = val

application = create_app()

manager = Manager(application)
migrate = Migrate(application, db)
manager.add_command('db', MigrateCommand)

if __name__ == '__main__':
    manager.run()
