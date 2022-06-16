"""

Revision ID: 79e3eab3daf3
Revises: 0373_add_notifications_view
Create Date: 2022-06-16 09:10:12.218526

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '79e3eab3daf3'
down_revision = '0373_add_notifications_view'


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('notifications_all_time_view',
    sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column('job_id', postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column('job_row_number', sa.Integer(), nullable=True),
    sa.Column('service_id', postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column('template_id', postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column('template_version', sa.Integer(), nullable=True),
    sa.Column('api_key_id', postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column('key_type', sa.String(), nullable=True),
    sa.Column('billable_units', sa.Integer(), nullable=True),
    sa.Column('notification_type', sa.Enum('email', 'sms', 'letter', name='notification_type'), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('sent_by', sa.String(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('notification_status', sa.Text(), nullable=True),
    sa.Column('reference', sa.String(), nullable=True),
    sa.Column('client_reference', sa.String(), nullable=True),
    sa.Column('international', sa.Boolean(), nullable=True),
    sa.Column('phone_prefix', sa.String(), nullable=True),
    sa.Column('rate_multiplier', sa.Numeric(asdecimal=False), nullable=True),
    sa.Column('created_by_id', postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column('postage', sa.String(), nullable=True),
    sa.Column('document_download_count', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.drop_table('broadcast_provider_message_status_type')
    op.alter_column('broadcast_message', 'content',
               existing_type=sa.TEXT(),
               type_=sa.String(),
               nullable=False)
    op.create_index(op.f('ix_inbound_sms_history_created_at'), 'inbound_sms_history', ['created_at'], unique=False)
    op.alter_column('notifications', 'international',
               existing_type=sa.BOOLEAN(),
               nullable=False)
    op.alter_column('rates', 'rate',
               existing_type=sa.NUMERIC(),
               type_=sa.Float(),
               existing_nullable=False)
    op.alter_column('services_history', 'prefix_sms',
               existing_type=sa.BOOLEAN(),
               nullable=False)
    op.alter_column('user_to_service', 'user_id',
               existing_type=postgresql.UUID(),
               nullable=False)
    op.alter_column('user_to_service', 'service_id',
               existing_type=postgresql.UUID(),
               nullable=False)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('user_to_service', 'service_id',
               existing_type=postgresql.UUID(),
               nullable=True)
    op.alter_column('user_to_service', 'user_id',
               existing_type=postgresql.UUID(),
               nullable=True)
    op.alter_column('services_history', 'prefix_sms',
               existing_type=sa.BOOLEAN(),
               nullable=True)
    op.alter_column('rates', 'rate',
               existing_type=sa.Float(),
               type_=sa.NUMERIC(),
               existing_nullable=False)
    op.alter_column('notifications', 'international',
               existing_type=sa.BOOLEAN(),
               nullable=True)
    op.drop_index(op.f('ix_inbound_sms_history_created_at'), table_name='inbound_sms_history')
    op.alter_column('broadcast_message', 'content',
               existing_type=sa.String(),
               type_=sa.TEXT(),
               nullable=True)
    op.create_table('broadcast_provider_message_status_type',
    sa.Column('name', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('name', name='broadcast_provider_message_status_type_pkey')
    )
    op.drop_table('notifications_all_time_view')
    # ### end Alembic commands ###
