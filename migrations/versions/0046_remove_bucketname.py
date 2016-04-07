"""empty message

Revision ID: 0046_remove_bucketname
Revises: 0045_template_stats_update_time
Create Date: 2016-04-07 12:23:55.050714

"""

# revision identifiers, used by Alembic.
revision = '0046_remove_bucketname'
down_revision = '0045_template_stats_update_time'

from alembic import op
import sqlalchemy as sa


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('jobs', 'file_name')
    op.drop_column('jobs', 'bucket_name')
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.add_column('jobs', sa.Column('bucket_name', sa.VARCHAR(), autoincrement=False, nullable=False))
    op.add_column('jobs', sa.Column('file_name', sa.VARCHAR(), autoincrement=False, nullable=False))
    ### end Alembic commands ###
