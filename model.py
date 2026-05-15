from datetime import datetime

from sqlalchemy import UniqueConstraint

from .setup import *


class ModelNaverToonItem(ModelBase):
    P = P
    __tablename__ = 'ne_toon_dl_item'
    __table_args__ = (
        UniqueConstraint('title_id', 'no', name='uq_ne_toon_dl_title_no'),
        {'mysql_collate': 'utf8_general_ci'},
    )
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    updated_time = db.Column(db.DateTime)

    # 작품/회차
    title_id = db.Column(db.Integer, index=True)
    title_name = db.Column(db.String)
    no = db.Column(db.Integer)            # 네이버웹툰 회차 번호 (article.no)
    episode_title = db.Column(db.String)  # subtitle
    page_count = db.Column(db.Integer)

    # 처리 상태: pending / downloading / completed / failed / partial / skipped_locked
    status = db.Column(db.String, index=True)
    error_msg = db.Column(db.String)

    # 파일 저장
    save_dir = db.Column(db.String)
    downloaded_count = db.Column(db.Integer)
    total_bytes = db.Column(db.BigInteger)
    downloaded_at = db.Column(db.DateTime)

    def __init__(self):
        self.created_time = datetime.now()
        self.updated_time = self.created_time
        self.status = 'pending'
        self.downloaded_count = 0
        self.total_bytes = 0
