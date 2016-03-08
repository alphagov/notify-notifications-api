"""empty message

Revision ID: 0036_notification_stats
Revises: 0035_default_sent_count
Create Date: 2016-03-08 11:16:25.659463

"""

# revision identifiers, used by Alembic.
revision = '0036_notification_stats'
down_revision = '0035_default_sent_count'

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

def upgrade():
    op.create_table('notification_statistics',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('day', sa.String(length=255), nullable=False),
    sa.Column('service_id', postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column('emails_requested', sa.BigInteger(), nullable=False),
    sa.Column('emails_delivered', sa.BigInteger(), nullable=True),
    sa.Column('emails_error', sa.BigInteger(), nullable=True),
    sa.Column('sms_requested', sa.BigInteger(), nullable=False),
    sa.Column('sms_delivered', sa.BigInteger(), nullable=True),
    sa.Column('sms_error', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('service_id', 'day', name='uix_service_to_day')
    )
    op.create_index(op.f('ix_service_notification_stats_service_id'), 'notification_statistics', ['service_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_service_notification_stats_service_id'), table_name='notification_statistics')
    op.drop_table('notification_statistics')
